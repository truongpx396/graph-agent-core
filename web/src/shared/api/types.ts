import type { components } from "./schema.gen";

export type Schemas = components["schemas"];
export type Citation = Schemas["Citation"];
export type ToolCall = Schemas["ToolCall"];
export type SessionSummary = Schemas["SessionSummary"];
export type SessionMessage = Schemas["SessionMessage"];
export type PendingApproval = Schemas["PendingApproval"];
export type UploadResult = Schemas["IngestUploadResult"];

export type ChatEvent =
  | Schemas["StatusEvent"]
  | Schemas["TokenEvent"]
  | Schemas["ToolStartEvent"]
  | Schemas["ToolEndEvent"]
  | Schemas["RetryEvent"]
  | Schemas["CompactedEvent"]
  | Schemas["CitationsEvent"]
  | Schemas["FollowupsEvent"]
  | Schemas["SystemNoteEvent"]
  | Schemas["UsageEvent"]
  | Schemas["ApprovalRequiredEvent"]
  | Schemas["DoneEvent"]
  | Schemas["ErrorEvent"];

export type IngestEvent =
  | Schemas["IngestStartedEvent"]
  | Schemas["IngestProgressEvent"]
  | Schemas["IngestDoneEvent"]
  | Schemas["IngestErrorEvent"];
