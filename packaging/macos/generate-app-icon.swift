import AppKit
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

guard CommandLine.arguments.count >= 2 else {
    fail("Usage: generate-app-icon.swift <output.iconset>")
}

let iconsetURL = URL(fileURLWithPath: CommandLine.arguments[1])
let fileManager = FileManager.default

let variants: [(name: String, pixels: Int)] = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]

func renderIcon(pixelSize: Int, to url: URL) throws {
    let dimension = CGFloat(pixelSize)
    let image = NSImage(size: NSSize(width: dimension, height: dimension))

    image.lockFocus()
    defer { image.unlockFocus() }

    let canvas = NSRect(x: 0, y: 0, width: dimension, height: dimension)
    NSColor.clear.setFill()
    canvas.fill()

    let padding = max(1, dimension * 0.08)
    let box = canvas.insetBy(dx: padding, dy: padding)
    let path = NSBezierPath(roundedRect: box, xRadius: dimension * 0.16, yRadius: dimension * 0.16)

    let top = NSColor(srgbRed: 0.060, green: 0.086, blue: 0.145, alpha: 1.0)
    let bottom = NSColor(srgbRed: 0.020, green: 0.034, blue: 0.075, alpha: 1.0)
    if let gradient = NSGradient(colors: [top, bottom]) {
        gradient.draw(in: path, angle: -45)
    } else {
        top.setFill()
        path.fill()
    }

    NSColor(srgbRed: 1, green: 1, blue: 1, alpha: 0.08).setStroke()
    path.lineWidth = max(1, dimension * 0.012)
    path.stroke()

    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: dimension * 0.35, weight: .bold),
        .foregroundColor: NSColor.white,
        .paragraphStyle: paragraph,
        .kern: -dimension * 0.006,
    ]
    let text = "DA" as NSString
    let textSize = text.size(withAttributes: attributes)
    let textRect = NSRect(
        x: box.midX - textSize.width / 2,
        y: box.midY - textSize.height / 2 - dimension * 0.012,
        width: textSize.width,
        height: textSize.height
    )
    text.draw(in: textRect, withAttributes: attributes)

    guard
        let tiff = image.tiffRepresentation,
        let bitmap = NSBitmapImageRep(data: tiff),
        let png = bitmap.representation(using: .png, properties: [:])
    else {
        fail("Failed to render DAClaw app icon PNG.")
    }

    try png.write(to: url)
}

do {
    try fileManager.removeItem(at: iconsetURL)
} catch CocoaError.fileNoSuchFile {
    // Nothing to clean.
} catch {
    fail("Failed to clean iconset: \(error)")
}

do {
    try fileManager.createDirectory(at: iconsetURL, withIntermediateDirectories: true)
    for variant in variants {
        try renderIcon(pixelSize: variant.pixels, to: iconsetURL.appendingPathComponent(variant.name))
    }
} catch {
    fail("Failed to generate DAClaw app icon: \(error)")
}
