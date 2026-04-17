import { firefox, } from "playwright-core";
import { launchOptions, syncAttachVD } from "./utils.js";
import { VirtualDisplay } from "./virtdisplay.js";
export async function Camoufox(launch_options = {}) {
    const { headless, user_data_dir, ...launchOptions } = launch_options;
    return NewBrowser(firefox, headless, {}, user_data_dir ?? false, false, launchOptions);
}
export async function NewBrowser(playwright, headless = false, fromOptions = {}, userDataDir = false, debug = false, launch_options = {}) {
    let virtualDisplay = null;
    if (headless === "virtual") {
        virtualDisplay = new VirtualDisplay(debug);
        launch_options.virtual_display = virtualDisplay.get();
        launch_options.headless = false;
    }
    else {
        launch_options.headless ||= headless;
    }
    if (!fromOptions || Object.keys(fromOptions).length === 0) {
        fromOptions = await launchOptions({ debug, ...launch_options });
    }
    if (typeof userDataDir === "string") {
        const context = await playwright.launchPersistentContext(userDataDir, fromOptions);
        return syncAttachVD(context, virtualDisplay);
    }
    const browser = await playwright.launch(fromOptions);
    return syncAttachVD(browser, virtualDisplay);
}
