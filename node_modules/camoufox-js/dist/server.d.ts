import { type BrowserServer } from "playwright-core";
import { type LaunchOptions } from "./utils.js";
export declare function launchServer({ port, ws_path, ...options }: LaunchOptions | {
    port?: number;
    ws_path?: string;
}): Promise<BrowserServer>;
