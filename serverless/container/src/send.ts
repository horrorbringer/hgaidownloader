import { openAsBlob } from "node:fs";
import { stat } from "node:fs/promises";

export interface SendOptions {
  token: string;
  chatId: number;
  filePath: string;
  fileName: string;
  caption: string;
  duration?: number;
  width?: number;
  height?: number;
}

export interface SendResult {
  ok: boolean;
  fileId?: string;
  description?: string;
  errorCode?: number;
}

async function upload(
  method: "sendVideo" | "sendDocument",
  options: SendOptions,
): Promise<SendResult> {
  const size = (await stat(options.filePath)).size;
  const form = new FormData();
  form.append("chat_id", String(options.chatId));
  form.append("caption", options.caption.slice(0, 1000));
  form.append("parse_mode", "HTML");
  if (method === "sendVideo") {
    form.append("supports_streaming", "true");
    if (options.duration) form.append("duration", String(Math.round(options.duration)));
    if (options.width) form.append("width", String(options.width));
    if (options.height) form.append("height", String(options.height));
  }
  const blob = await openAsBlob(options.filePath, { type: "video/mp4" });
  form.append(method === "sendVideo" ? "video" : "document", blob, options.fileName);

  try {
    const response = await fetch(
      `https://api.telegram.org/bot${options.token}/${method}`,
      {
        method: "POST",
        body: form,
        signal: AbortSignal.timeout(300_000),
      },
    );
    const data = (await response.json().catch(() => null)) as
      | {
          ok?: boolean;
          result?: { file_id?: string };
          description?: string;
          error_code?: number;
        }
      | null;
    if (!data) {
      return {
        ok: false,
        description: `Telegram returned HTTP ${response.status} after ${size} bytes`,
      };
    }
    return {
      ok: Boolean(data.ok),
      fileId: data.result?.file_id,
      description: data.description,
      errorCode: data.error_code,
    };
  } catch (error) {
    return {
      ok: false,
      description: error instanceof Error ? error.message : String(error),
    };
  }
}

export async function sendVideo(options: SendOptions): Promise<SendResult> {
  return upload("sendVideo", options);
}

/** Used when Telegram refuses the inline video, the file is still delivered. */
export async function sendDocument(options: SendOptions): Promise<SendResult> {
  return upload("sendDocument", options);
}
