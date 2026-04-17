import { firefox } from "playwright-core";
import { launchOptions } from "./utils.js";
export async function launchServer({ port, ws_path, ...options }) {
    return firefox.launchServer({
        ...(await launchOptions(options)),
        port,
        wsPath: ws_path,
    });
}
