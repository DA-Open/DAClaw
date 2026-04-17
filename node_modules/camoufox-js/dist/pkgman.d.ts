import type { PathLike } from "node:fs";
import type { Writable } from "node:stream";
export declare const OS_NAME: "mac" | "win" | "lin";
export declare const INSTALL_DIR: PathLike;
export declare const LOCAL_DATA: PathLike;
export declare const OS_ARCH_MATRIX: {
    [key: string]: string[];
};
declare class Version {
    release: string;
    version?: string;
    sorted_rel: number[];
    constructor(release: string, version?: string);
    private buildSortedRel;
    get fullString(): string;
    equals(other: Version): boolean;
    lessThan(other: Version): boolean;
    isSupported(): boolean;
    static fromPath(filePath?: PathLike): Version;
    static isSupportedPath(path: PathLike): boolean;
    static buildMinMax(): [Version, Version];
}
export declare class GitHubDownloader {
    githubRepo: string;
    apiUrl: string;
    constructor(githubRepo: string);
    checkAsset(asset: any): any;
    missingAssetError(): void;
    getAsset({ retries }?: {
        retries: number;
    }): Promise<any>;
}
export declare class CamoufoxFetcher extends GitHubDownloader {
    arch: string;
    _version_obj?: Version;
    pattern: RegExp;
    _url?: string;
    constructor();
    init(): Promise<void>;
    checkAsset(asset: any): [Version, string] | null;
    missingAssetError(): void;
    static getPlatformArch(): string;
    fetchLatest(): Promise<void>;
    static downloadFile(url: string): Promise<Buffer>;
    extractZip(zipFile: string | Buffer): Promise<void>;
    static cleanup(): boolean;
    setVersion(): void;
    install(): Promise<void>;
    get url(): string;
    get version(): string;
    get release(): string;
    get verstr(): string;
}
export declare function installedVerStr(): string;
export declare function camoufoxPath(downloadIfMissing?: boolean): PathLike;
export declare function getPath(file: string): string;
export declare function launchPath(): string;
export declare function webdl(url: string, desc?: string, bar?: boolean, buffer?: Writable | null, { retries }?: {
    retries: number;
}): Promise<Buffer>;
export declare function unzip(zipFile: Buffer, extractPath: string, desc?: string, bar?: boolean): Promise<void>;
export {};
