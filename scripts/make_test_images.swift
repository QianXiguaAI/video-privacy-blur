import Foundation
import AppKit
import CoreImage

let directory=URL(fileURLWithPath:CommandLine.arguments[1])
try FileManager.default.createDirectory(at:directory,withIntermediateDirectories:true)
for (index,email) in ["demo.us","demo.user@example.com","demo.user@example.com"].enumerated() {
    let bitmap=NSBitmapImageRep(bitmapDataPlanes:nil,pixelsWide:480,pixelsHigh:600,bitsPerSample:8,samplesPerPixel:4,hasAlpha:true,isPlanar:false,colorSpaceName:.deviceRGB,bytesPerRow:0,bitsPerPixel:0)!
    let graphics=NSGraphicsContext(bitmapImageRep:bitmap)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current=graphics
    NSColor(calibratedRed:0.10,green:0.12,blue:0.13,alpha:1).setFill()
    NSRect(x:0,y:0,width:480,height:600).fill()
    let lines=["SYNTHETIC PRIVACY TEST",email,"Reply: "+email,"Name: Sample User","Phone: 13800001234","Verification code: 731946","Order: 50678901234567890123"]
    for (row,text) in lines.enumerated() {
        let attrs:[NSAttributedString.Key:Any]=[.font:NSFont.monospacedSystemFont(ofSize:row==0 ? 18 : 20,weight:.regular),.foregroundColor:NSColor.white]
        NSAttributedString(string:text,attributes:attrs).draw(at:NSPoint(x:28,y:410-row*43))
    }
    if index==2 {
        NSAttributedString(string:"demo.user@example.com",attributes:[.font:NSFont.systemFont(ofSize:9),.foregroundColor:NSColor.white]).draw(at:NSPoint(x:240,y:130))
    }
    NSGraphicsContext.restoreGraphicsState()
    try bitmap.representation(using:.png,properties:[:])!.write(to:directory.appendingPathComponent("phase\(index).png"))
}
for payment in [true,false] {
    let bitmap=NSBitmapImageRep(bitmapDataPlanes:nil,pixelsWide:640,pixelsHigh:480,bitsPerSample:8,samplesPerPixel:4,hasAlpha:true,isPlanar:false,colorSpaceName:.deviceRGB,bytesPerRow:0,bitsPerPixel:0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current=NSGraphicsContext(bitmapImageRep:bitmap)
    NSColor(calibratedWhite:0.12,alpha:1).setFill()
    NSRect(x:0,y:0,width:640,height:480).fill()
    let attrs:[NSAttributedString.Key:Any]=[.font:NSFont.systemFont(ofSize:26),.foregroundColor:NSColor.white]
    NSAttributedString(string:payment ? "付款碼" : "商品條碼",attributes:attrs).draw(at:NSPoint(x:40,y:390))
    if payment { NSAttributedString(string:"被保險人：陳小明",attributes:attrs).draw(at:NSPoint(x:40,y:335)) }
    let generator=CIFilter(name:"CICode128BarcodeGenerator")!
    generator.setValue(Data("123456789012345678".utf8),forKey:"inputMessage")
    generator.setValue(12,forKey:"inputQuietSpace")
    let code=generator.outputImage!
    let cg=CIContext().createCGImage(code,from:code.extent)!
    NSColor.white.setFill();NSRect(x:45,y:155,width:550,height:120).fill()
    NSImage(cgImage:cg,size:code.extent.size).draw(in:NSRect(x:60,y:165,width:520,height:100))
    NSAttributedString(string:"123456789012345678",attributes:attrs).draw(at:NSPoint(x:70,y:95))
    NSGraphicsContext.restoreGraphicsState()
    try bitmap.representation(using:.png,properties:[:])!.write(to:directory.appendingPathComponent(payment ? "payment.png" : "product.png"))
}
print("Created synthetic test images")
