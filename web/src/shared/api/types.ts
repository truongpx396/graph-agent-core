import type { components } from "./schema.gen";

export type Schemas = components["schemas"];
export type Citation = Schemas["Citation"];
export type ToolCall = Schemas["ToolCall"];
export type SessionSummary = Schemas["SessionSummary"];
export type SessionMessage = Schemas["SessionMessage"];
export type PendingApproval = Schemas["PendingApproval"];
export type UploadResult = Schemas["IngestUploadResult"];

// Sent by the stream (app/agent/runtime/stream.py, when an app update strands a pending approval) but missing from the OpenAPI document's
// union. Adding it there is a "breaking" change under this repo's gate, so it is typed by hand until the next major version.
export interface SystemNoteEvent {
  type: "system_note";
  content: string;
}

export type ChatEvent =
  | Schemas["TokenEvent"]
  | Schemas["ToolStartEvent"]
  | Schemas["ToolEndEvent"]
  | Schemas["RetryEvent"]
  | Schemas["CompactedEvent"]
  | Schemas["CitationsEvent"]
  | Schemas["FollowupsEvent"]
  | Schemas["ApprovalRequiredEvent"]
  | Schemas["DoneEvent"]
  | Schemas["ErrorEvent"]
  | SystemNoteEvent;

export type IngestEvent =
  | Schemas["IngestStartedEvent"]
  | Schemas["IngestProgressEvent"]
  | Schemas["IngestDoneEvent"]
  | Schemas["IngestErrorEvent"];
