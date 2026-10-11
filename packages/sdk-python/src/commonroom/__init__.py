"""Python SDK for connecting an AI companion to a commonroom world."""

from commonroom_protocol import PROTOCOL_VERSION, Reason

from .client import Agent, FatalError, Result
from .tools import call_tool, describe_result, tool_schemas
from .view import Perception, format_event

__all__ = ["Agent", "FatalError", "Result", "Perception", "format_event", "tool_schemas", "call_tool",
           "describe_result", "PROTOCOL_VERSION", "Reason"]
