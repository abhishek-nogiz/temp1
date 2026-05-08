"""
Long workflow execution with state recovery, checkpointing,
and step-by-step execution.
"""
import time
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from ..service import WebIntelService
from ..robust.state import StateManager
from ..robust.retries import with_retries, RetryConfig


@dataclass
class WorkflowStep:
    name: str
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    max_retries: int = 3
    checkpoint: bool = False


@dataclass
class WorkflowResult:
    success: bool
    steps_completed: int
    total_steps: int
    outputs: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    final_url: Optional[str] = None
    screenshots: List[str] = field(default_factory=list)


class WorkflowExecutor:
    """
    Executes multi-step web workflows with:
    - Checkpointing (save state between steps)
    - Recovery (resume from last checkpoint)
    - Step-by-step logging
    - Screenshot capture
    """

    def __init__(self, service: WebIntelService, state_manager: Optional[StateManager] = None):
        self.service = service
        self.state = state_manager or StateManager()
        self.workflow_id: Optional[str] = None

    def execute(self, workflow_id: str, steps: List[WorkflowStep], resume: bool = False) -> WorkflowResult:
        self.workflow_id = workflow_id
        result = WorkflowResult(
            success=False,
            steps_completed=0,
            total_steps=len(steps),
            outputs={},
            errors=[],
        )

        start_index = 0
        if resume:
            saved = self.state.load(workflow_id)
            if saved and saved.get("metadata", {}).get("last_step") is not None:
                start_index = saved["metadata"]["last_step"] + 1
                print(f"[Workflow] Resuming from step {start_index}")
                self.state.restore(workflow_id, self.service.session.page, self.service.session.context)

        for i, step in enumerate(steps[start_index:], start=start_index):
            print(f"[Workflow] Step {i + 1}/{len(steps)}: {step.name} ({step.action})")

            try:
                output = self._execute_step(step)
                result.outputs[step.name] = output
                result.steps_completed = i + 1

                if step.checkpoint:
                    self.state.save(
                        workflow_id,
                        self.service.session.page,
                        metadata={"last_step": i, "step_name": step.name},
                    )
                    print(f"[Workflow] Checkpoint saved at step {i}")

                if step.action in ["click", "extract", "screenshot"]:
                    path = f"{workflow_id}_step_{i}.png"
                    self.service.screenshot(path)
                    result.screenshots.append(path)

            except Exception as e:
                error_msg = f"Step {i} ({step.name}) failed: {str(e)}"
                print(f"[Workflow] ERROR: {error_msg}")
                result.errors.append(error_msg)

                self.state.save(
                    workflow_id,
                    self.service.session.page,
                    metadata={"last_step": i - 1, "failed_step": i, "error": str(e)},
                )
                return result

        result.success = True
        result.final_url = self.service.session.page.url
        return result

    def _execute_step(self, step: WorkflowStep) -> Any:
        retry_config = RetryConfig(
            max_retries=step.max_retries,
            screenshot_on_failure=True,
            heal_on_failure=True,
        )
        @with_retries(retry_config)
        def _run(_service):      # <-- accept a dummy argument so the wrapper can receive the service
            if step.action == "open":
                return self.service.open_page(step.target or step.value)

            elif step.action == "click":
                return self.service.click_element(step.target)

            elif step.action == "type":
                return self.service.type_text(step.target, step.value or "")

            elif step.action == "scroll":
                self.service.session.human_like_scroll(int(step.value or 800))
                return {"scrolled": True}

            elif step.action == "extract":
                return self.service.extract_detail()

            elif step.action == "wait":
                time.sleep(float(step.value or 2))
                return {"waited": step.value}

            elif step.action == "screenshot":
                path = step.value or f"screenshot_{int(time.time())}.png"
                return self.service.screenshot(path)

            elif step.action == "find":
                return self.service.find_element(step.target)

            elif step.action == "aql":
                return self.service.query_aql(step.target)

            else:
                raise ValueError(f"Unknown action: {step.action}")

        # Call the wrapped function, passing the service so the retry decorator can see it
        return _run(self.service)