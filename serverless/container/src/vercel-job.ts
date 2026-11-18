import { createJobHandle } from "./handle.js";
import { runJob, type JobRequest } from "./job.js";

const payload = process.env.MEDIA_JOB;
delete process.env.MEDIA_JOB;
if (!payload) throw new Error("MEDIA_JOB is required");
await runJob(JSON.parse(payload) as JobRequest, createJobHandle());
