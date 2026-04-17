export declare const ALLOW_GEOIP = true;
declare class Locale {
    language: string;
    region?: string | undefined;
    script?: string | undefined;
    constructor(language: string, region?: string | undefined, script?: string | undefined);
    asString(): string;
    asConfig(): Record<string, string>;
}
declare class Geolocation {
    locale: Locale;
    longitude: number;
    latitude: number;
    timezone: string;
    accuracy?: number | undefined;
    constructor(locale: Locale, longitude: number, latitude: number, timezone: string, accuracy?: number | undefined);
    asConfig(): Record<string, any>;
}
export declare function normalizeLocale(locale: string): Locale;
export declare function handleLocale(locale: string, ignoreRegion?: boolean): Locale;
export declare function handleLocales(locales: string | string[], config: Record<string, any>): void;
export declare function geoipAllowed(): void;
export declare function downloadMMDB(): Promise<void>;
export declare function removeMMDB(): void;
export declare function getGeolocation(ip: string): Promise<Geolocation>;
export {};
