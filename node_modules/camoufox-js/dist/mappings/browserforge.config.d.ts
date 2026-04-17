/**
 * Mappings of Browserforge fingerprints to Camoufox config properties.
 */
declare const _default: {
    navigator: {
        userAgent: string;
        doNotTrack: string;
        appCodeName: string;
        appName: string;
        appVersion: string;
        oscpu: string;
        platform: string;
        hardwareConcurrency: string;
        product: string;
        maxTouchPoints: string;
        extraProperties: {
            globalPrivacyControl: string;
        };
    };
    screen: {
        availLeft: string;
        availTop: string;
        availWidth: string;
        availHeight: string;
        height: string;
        width: string;
        colorDepth: string;
        pixelDepth: string;
        pageXOffset: string;
        pageYOffset: string;
        outerHeight: string;
        outerWidth: string;
        innerHeight: string;
        innerWidth: string;
        screenX: string;
        screenY: string;
    };
    headers: {
        "Accept-Encoding": string;
    };
    battery: {
        charging: string;
        chargingTime: string;
        dischargingTime: string;
    };
};
export default _default;
