import type { JobRunner } from "./jobs";

export interface Env {
  runner: JobRunner;
  defer?: (work: Promise<void>) => void;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_WEBHOOK_SECRET: string;
  API_KEY: string;
  INTERNAL_SECRET: string;
  ALLOWED_USER_IDS: string;
  ALLOWED_CHAT_IDS?: string;
  ALLOWED_HOSTS: string;
  PUBLIC_BASE_URL?: string;
  DEFAULT_CHAT_ID?: string;
}
