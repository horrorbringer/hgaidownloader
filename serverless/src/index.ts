import app from "./app";
import type { Env } from "./env";
import { Runner } from "./runner";

export { Runner };

type Bindings = Omit<Env, "runner" | "defer"> & {
  RUNNER: DurableObjectNamespace<Runner>;
};

export default {
  fetch(request: Request, env: Bindings, context: ExecutionContext) {
    return app.fetch(request, {
      ...env,
      runner: env.RUNNER.get(env.RUNNER.idFromName("global")),
      defer: (work) => context.waitUntil(work),
    });
  },
};
