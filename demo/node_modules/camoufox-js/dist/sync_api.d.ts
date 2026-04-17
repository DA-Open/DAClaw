import { type Browser, type BrowserContext, type BrowserType } from "playwright-core";
import { type LaunchOptions } from "./utils.js";
export declare function Camoufox<UserDataDir extends string | undefined = undefined, ReturnType = UserDataDir extends string ? BrowserContext : Browser>(launch_options?: LaunchOptions | {
    headless?: boolean | "virtual";
    user_data_dir: UserDataDir;
}): Promise<ReturnType>;
export declare function NewBrowser<UserDataDir extends string | false = false, ReturnType = UserDataDir extends string ? BrowserContext : Browser>(playwright: BrowserType<Browser>, headless?: boolean | "virtual", fromOptions?: Record<string, any>, userDataDir?: UserDataDir, debug?: boolean, launch_options?: LaunchOptions): Promise<ReturnType>;
