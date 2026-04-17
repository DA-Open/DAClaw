interface WebGLData {
    vendor: string;
    renderer: string;
    data: string;
    win: number;
    mac: number;
    lin: number;
    webGl2Enabled: boolean;
}
export declare function sampleWebGL(os: "win" | "mac" | "lin", vendor?: string, renderer?: string): Promise<WebGLData>;
interface VendorRenderer {
    vendor: string;
    renderer: string;
}
interface PossiblePairs {
    [key: string]: Array<VendorRenderer>;
}
export declare function getPossiblePairs(): Promise<PossiblePairs>;
export {};
