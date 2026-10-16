import Foundation
import AVFoundation
import Vision
import CoreImage

// Arguments: input, JSONL output, sampling seconds (0 = every decoded frame).
// stdout contains progress counts only. Recognized text stays in the local cache.
guard CommandLine.arguments.count >= 3 else { fatalError("Expected input and output paths") }
let input=URL(fileURLWithPath:CommandLine.arguments[1])
let destination=URL(fileURLWithPath:CommandLine.arguments[2])
let interval=CommandLine.arguments.count>3 ? Double(CommandLine.arguments[3]) ?? 0 : 0
let rangeStart=CommandLine.arguments.count>4 ? Double(CommandLine.arguments[4]) ?? 0 : 0
let rangeEnd=CommandLine.arguments.count>5 ? Double(CommandLine.arguments[5]) ?? .infinity : .infinity
let expectedFirstTime=CommandLine.arguments.count>7 ? Double(CommandLine.arguments[7]) ?? rangeStart : rangeStart
let requestedLanguages=CommandLine.arguments.count>6 ? CommandLine.arguments[6].split(separator:",").map(String.init) : ["zh-Hans","zh-Hant","en-US"]
let languageCheck=VNRecognizeTextRequest()
languageCheck.recognitionLevel = .accurate
let supportedLanguages=try languageCheck.supportedRecognitionLanguages()
guard !requestedLanguages.isEmpty,requestedLanguages.allSatisfy({supportedLanguages.contains($0)}) else {
    fatalError("Requested OCR languages are not supported by this Vision revision")
}
let asset=AVURLAsset(url:input)
guard let track=asset.tracks(withMediaType:.video).first else { fatalError("No video stream") }
let transform=track.preferredTransform
let context=CIContext(options:[.cacheIntermediates:false])
let reader=try AVAssetReader(asset:asset)
if rangeStart>0 || rangeEnd.isFinite {
    let readerStart=max(0,rangeStart-0.01)
    let start=CMTime(seconds:readerStart,preferredTimescale:1000000)
    let duration=rangeEnd.isFinite ? CMTime(seconds:rangeEnd-readerStart,preferredTimescale:1000000) : CMTime.positiveInfinity
    reader.timeRange=CMTimeRange(start:start,duration:duration)
}
let stream=AVAssetReaderTrackOutput(track:track,outputSettings:[kCVPixelBufferPixelFormatTypeKey as String:kCVPixelFormatType_32BGRA])
stream.alwaysCopiesSampleData=false
reader.add(stream)
guard reader.startReading() else { fatalError("Cannot start video reader") }
guard FileManager.default.createFile(atPath:destination.path,contents:nil,attributes:[.posixPermissions:0o600]) else { fatalError("Cannot create scan cache") }
let file=try FileHandle(forWritingTo:destination)
let tokenizer=try NSRegularExpression(pattern:"[A-Za-z0-9][A-Za-z0-9._%+@/：:\\-]*|[\\p{Han}]{1,8}")
// Include exact ranges for common fields, beyond the generic token geometry.
let fieldPatterns=try [
    "[A-Za-z0-9._%+\\-]+\\s*@\\s*[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}",
    "(?<![0-9])(?:[0-9][ \\-]?){3,}[0-9](?![0-9])",
    "(?:姓名|投保人|被保险人|被保險人|被保人|联系人|聯絡人|聯繫人|收件人|持卡人)[：: ]+([\\p{Han}]{2,4})"
].map { try NSRegularExpression(pattern:$0) }
func pixelBox(_ b:CGRect,_ width:Int,_ height:Int)->[Double] {
    [b.minX*Double(width),(1-b.maxY)*Double(height),b.maxX*Double(width),(1-b.minY)*Double(height)]
}
var frame=0,scanned=0
var nextTime = -Double.infinity
while let sample=stream.copyNextSampleBuffer() {
    try autoreleasepool {
        guard let pixel=CMSampleBufferGetImageBuffer(sample) else { throw NSError(domain:"Missing video pixel buffer",code:1) }
        let time=CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sample))
        // A ranged AVAssetReader can synthesize a clipped edit-list sample at
        // rangeStart. It is not one of the source presentation timestamps.
        // Keep the existing 2 ms AVFoundation/ffprobe alignment tolerance. A
        // 10 ms pre-roll keeps synthetic clipped samples outside that window.
        if time<expectedFirstTime-0.002 || time>=rangeEnd { return }
        if interval>0 && time+0.000001<nextTime { return }
        nextTime=time+interval
        var width=CVPixelBufferGetWidth(pixel),height=CVPixelBufferGetHeight(pixel)
        var handler:VNImageRequestHandler
        if transform.isIdentity {
            handler=VNImageRequestHandler(cvPixelBuffer:pixel,options:[:])
        } else {
            // AV preferredTransform uses raster (top-left) axes; Core Image uses
            // bottom-left axes. Conjugate the linear part by the Y-axis flip.
            let displayTransform=CGAffineTransform(a:transform.a,b:-transform.b,c:-transform.c,d:transform.d,tx:0,ty:0)
            let transformed=CIImage(cvPixelBuffer:pixel).transformed(by:displayTransform)
            let extent=transformed.extent.integral
            let upright=transformed.transformed(by:CGAffineTransform(translationX:-extent.minX,y:-extent.minY))
            width=Int(extent.width);height=Int(extent.height)
            guard let image=context.createCGImage(upright,from:CGRect(x:0,y:0,width:width,height:height)) else { throw NSError(domain:"Orientation normalization failed",code:2) }
            handler=VNImageRequestHandler(cgImage:image,options:[:])
        }
        let text=VNRecognizeTextRequest()
        text.recognitionLevel = .accurate
        text.recognitionLanguages=requestedLanguages
        text.usesLanguageCorrection=true
        text.minimumTextHeight=0.004
        let codes=VNDetectBarcodesRequest()
        codes.symbologies=[.qr,.aztec,.pdf417,.code128,.ean13]
        try handler.perform([text,codes])
        var observations=[[String:Any]]()
        for observation in text.results ?? [] {
            guard let candidate=observation.topCandidates(1).first else { continue }
            let value=candidate.string
            let whole=NSRange(value.startIndex...,in:value)
            var ranges=tokenizer.matches(in:value,range:whole).map{$0.range}
            for pattern in fieldPatterns {
                for match in pattern.matches(in:value,range:whole) {
                    ranges.append(match.numberOfRanges>1 ? match.range(at:1) : match.range)
                }
            }
            var tokens=[[String:Any]](),seen=Set<String>()
            for span in ranges {
                let key="\(span.location):\(span.length)"
                guard !seen.contains(key),let range=Range(span,in:value),let result=try? candidate.boundingBox(for:range) else { continue }
                seen.insert(key)
                tokens.append(["start":span.location,"end":span.location+span.length,"box":pixelBox(result.boundingBox,width,height)])
            }
            observations.append(["text":value,"confidence":candidate.confidence,"box":pixelBox(observation.boundingBox,width,height),"tokens":tokens])
        }
        let barcodes=(codes.results ?? []).map { ["box":pixelBox($0.boundingBox,width,height),"confidence":$0.confidence,"symbology":$0.symbology.rawValue] as [String:Any] }
        let record:[String:Any]=["frame":frame,"pts":time,"width":width,"height":height,"texts":observations,"codes":barcodes,"languages":requestedLanguages]
        try file.write(contentsOf:JSONSerialization.data(withJSONObject:record,options:[.sortedKeys]))
        try file.write(contentsOf:Data([10]))
        scanned+=1
        if scanned%300==0 { print("OCR frames: \(scanned)");fflush(stdout) }
    }
    frame+=1
}
try file.close()
guard reader.status == .completed else { fatalError("Incomplete scan: \(String(describing:reader.error))") }
print("Decoded \(frame) frames; scanned \(scanned)")
