export type JobKind = "resolve" | "download";
export type JobStatus =
  | "queued"
  | "starting"
  | "running"
  | "cancelling"
  | "done"
  | "failed"
  | "cancelled";

export interface JobInput {
  jobId: string;
  kind: JobKind;
  url: string;
  chatId: number | null;
  messageId: number | null;
  episodes: number[] | null;
  callbackBase: string;
}

/** Container -> worker progress event. All fields are optional except `type`. */
export interface JobEvent {
  type: "stage" | "resolved" | "episode" | "done";
  title?: string;
  episodes?: Array<{ number: number; label: string }>;
  note?: string;
  number?: number;
  label?: string;
  state?: string;
  percent?: number;
  detail?: string;
  bytes?: number;
  fileId?: string;
  error?: string;
  ok?: boolean;
  cancelled?: boolean;
  summary?: string;
}

export interface JobRunner {
  seenUpdate(updateId: number): Promise<boolean>;
  enqueue(input: JobInput): Promise<{ queued: number }>;
  jobView(jobId: string): Promise<Record<string, unknown> | null>;
  statusText(chatId: number): Promise<string>;
  cancelChat(chatId: number): Promise<string>;
  cancelJob(jobId: string): Promise<boolean>;
  handleEvent(jobId: string, event: JobEvent): Promise<void>;
}
