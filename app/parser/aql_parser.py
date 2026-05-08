import re
from typing import Dict, Any, List, Optional


class AQLQuery:
    def __init__(self, root_name: str, is_list: bool, fields: Dict[str, Any]):
        self.root_name = root_name
        self.is_list = is_list
        self.fields = fields

    def __repr__(self):
        list_marker = "[]" if self.is_list else ""
        return f"AQLQuery({self.root_name}{list_marker}, fields={list(self.fields.keys())})"


class AQLParser:
    @staticmethod
    def parse(query_str: str) -> AQLQuery:
        query_str = query_str.strip()
        if query_str.startswith("{") and query_str.endswith("}"):
            query_str = query_str[1:-1].strip()

        pattern = r"^(\w+)(\[\])?\s*\{\s*(.*?)\s*\}$"
        match = re.match(pattern, query_str, re.DOTALL)

        if not match:
            if "{" not in query_str:
                fields = {f.strip(): None for f in query_str.split(",") if f.strip()}
                return AQLQuery("items", True, fields)
            raise ValueError(f"Invalid AQL query: {query_str}")

        root_name = match.group(1)
        is_list = match.group(2) is not None
        fields_str = match.group(3)

        fields = AQLParser._parse_fields(fields_str)
        return AQLQuery(root_name, is_list, fields)

    @staticmethod
    def _parse_fields(fields_str: str) -> Dict[str, Any]:
        fields = {}
        depth = 0
        current = []

        for char in fields_str:
            if char == "{":
                depth += 1
                current.append(char)
            elif char == "}":
                depth -= 1
                current.append(char)
            elif char == "," and depth == 0:
                field = "".join(current).strip()
                if field:
                    name, subquery = AQLParser._parse_field(field)
                    fields[name] = subquery
                current = []
            else:
                current.append(char)

        field = "".join(current).strip()
        if field:
            name, subquery = AQLParser._parse_field(field)
            fields[name] = subquery

        return fields

    @staticmethod
    def _parse_field(field_str: str) -> tuple:
        field_str = field_str.strip()
        nested_pattern = r"^(\w+)\s*\{\s*(.*?)\s*\}$"
        match = re.match(nested_pattern, field_str, re.DOTALL)
        if match:
            name = match.group(1)
            sub_fields_str = match.group(2)
            sub_fields = AQLParser._parse_fields(sub_fields_str)
            return name, AQLQuery(name, False, sub_fields)

        hint_pattern = r"^(\w+)\s*(?:\((.*?)\))?$"
        match = re.match(hint_pattern, field_str)
        if match:
            name = match.group(1)
            hint = match.group(2)
            return name, hint

        return field_str, None

    @staticmethod
    def to_extraction_plan(query: AQLQuery) -> Dict[str, Any]:
        plan = {
            "root": query.root_name,
            "is_list": query.is_list,
            "fields": {},
        }

        for field_name, subquery in query.fields.items():
            if isinstance(subquery, AQLQuery):
                plan["fields"][field_name] = AQLParser.to_extraction_plan(subquery)
            else:
                plan["fields"][field_name] = {
                    "type": "text",
                    "hint": subquery,
                }

        return plan


def parse_aql(query_str: str) -> Dict[str, Any]:
    parser = AQLParser()
    query = parser.parse(query_str)
    return parser.to_extraction_plan(query)
