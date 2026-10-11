"""Python SDK for connecting an AI companion to a commonroom world."""

from commonroom_protocol import PROTOCOL_VERSION, Reason

from .avatar import AvatarUploadError, remove_avatar, upload_avatar
from .client import Agent, FatalError, Result
from .tools import call_tool, describe_result, is_failure, openai_tool_schemas, tool_schemas
from .view import Perception, format_event, format_map

__all__ = ["Agent", "FatalError", "Result", "Perception", "format_event", "format_map", "tool_schemas", "openai_tool_schemas",
           "call_tool", "describe_result", "is_failure", "AvatarUploadError", "upload_avatar", "remove_avatar", "PROTOCOL_VERSION", "Reason"]
