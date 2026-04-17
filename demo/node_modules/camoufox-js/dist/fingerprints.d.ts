import { type Fingerprint, type FingerprintGeneratorOptions } from "fingerprint-generator";
export declare const SUPPORTED_OS: readonly ["linux", "macos", "windows"];
export declare function fromBrowserforge(fingerprint: Fingerprint, ffVersion?: string): Record<string, any>;
export declare function generateFingerprint(window?: [number, number], config?: Partial<FingerprintGeneratorOptions>): Fingerprint;
