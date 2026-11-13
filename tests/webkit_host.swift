// The dashboard in a WKWebView, the engine of the app's own window (accountdeck/platform/dashboard_window.py),
// driven by JSON lines on stdin. Each command gets one JSON line back on stdout. tests/support.py builds and
// starts it; nothing here reaches a real account, and the page's storage is thrown away with the process.
//
//   webkit_host [app|browser]               drive a page (app: the user agent of the app's window)
//   webkit_host diff <a.png> <b.png> <out>  count the pixels that differ and write the differing bands, a over b
//
// Commands: {"op":"load","url"} {"op":"js","expression"} {"op":"click","x","y"} {"op":"press","key"}
// {"op":"size","width","height"} {"op":"dark","on"} {"op":"shot","path","scale"} {"op":"problems"}.
import AppKit
import WebKit

let appAgent = "AccountDeck2App/2"              // USER_AGENT_NAME in dashboard_window.py: what the page checks for
let browserAgent = "Version/27.0 Safari/605.1.15"
let defaultSize = NSSize(width: 1180, height: 820)   // DEFAULT_SIZE in dashboard_window.py

// What the page's console would show in red, sent to the host as it happens.
let watchConsole = """
(() => {
  const send = (text) => { try { window.webkit.messageHandlers.host.postMessage(String(text)); } catch (error) {} };
  const words = (values) => values.map((value) => (value && value.stack) ? `${value}\\n${value.stack}` : String(value)).join(' ');
  const error = console.error.bind(console), assert = console.assert.bind(console);
  console.error = (...values) => { send(words(values)); error(...values); };
  console.assert = (ok, ...values) => { if (!ok) send('Assertion failed: ' + words(values)); assert(ok, ...values); };
  window.addEventListener('error', (event) => {
    const from = event.target && (event.target.src || event.target.href);
    send(event.error ? words([event.error]) : event.message || `could not load ${from || 'a file'}`);
  }, true);
  window.addEventListener('unhandledrejection', (event) => send('Unhandled rejection: ' + words([event.reason])));
  document.addEventListener('securitypolicyviolation', (event) =>
    send(`Refused by the Content-Security-Policy: ${event.violatedDirective} ${event.blockedURI}`));
})();
"""

// A window WebKit treats as the active one without it ever showing on the screen or taking the keyboard.
final class HostWindow: NSWindow {
    override var isKeyWindow: Bool { true }
    override var isMainWindow: Bool { true }
    override var canBecomeKey: Bool { true }
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect { frameRect }
}

func answer(_ object: [String: Any]) {
    let data = (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{\"error\":\"unprintable answer\"}".utf8)
    FileHandle.standardOutput.write(data + Data("\n".utf8))
}

final class Host: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
    let window: HostWindow
    let web: WKWebView
    var problems: [String] = []
    var committed: ((String?) -> Void)?
    var events = 0
    var loads = 0

    init(agent: String) {
        let configuration = WKWebViewConfiguration()
        configuration.applicationNameForUserAgent = agent
        configuration.websiteDataStore = .nonPersistent()
        let frame = NSRect(origin: NSPoint(x: -20000, y: -20000), size: defaultSize)
        window = HostWindow(contentRect: frame, styleMask: [.borderless], backing: .buffered, defer: false)
        web = WKWebView(frame: NSRect(origin: .zero, size: defaultSize), configuration: configuration)
        super.init()
        configuration.userContentController.add(self, name: "host")
        configuration.userContentController.addUserScript(
            WKUserScript(source: watchConsole, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web.navigationDelegate = self
        web.autoresizingMask = [.width, .height]
        // Off the screen counts as covered, and a covered page stops its timers. The page must behave as shown.
        web.setValue(false, forKey: "windowOcclusionDetectionEnabled")
        window.isReleasedWhenClosed = false
        window.contentView?.addSubview(web)
        window.appearance = NSAppearance(named: .aqua)
        window.orderFrontRegardless()
        window.makeFirstResponder(web)
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        problems.append(message.body as? String ?? "\(message.body)")
    }

    // ---- loading: answered once the new page has replaced the old one ----
    func settle(_ error: String?) {
        let waiting = committed
        committed = nil
        waiting?(error)
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) { settle(nil) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        settle(error.localizedDescription)
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        settle(error.localizedDescription)
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        problems.append("The web content process ended.")
        settle("The web content process ended.")
    }

    // ---- a person's hand ----
    func inWindow(_ x: Double, _ y: Double) -> NSPoint {
        web.convert(NSPoint(x: x, y: web.isFlipped ? y : Double(web.bounds.height) - y), to: nil)
    }

    func mouse(_ type: NSEvent.EventType, _ place: NSPoint, clicks: Int) -> NSEvent? {
        events += 1
        return NSEvent.mouseEvent(with: type, location: place, modifierFlags: [],
                                  timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber,
                                  context: nil, eventNumber: events, clickCount: clicks,
                                  pressure: type == .leftMouseDown ? 1 : 0)
    }

    func click(_ x: Double, _ y: Double) {
        let place = inWindow(x, y)
        if let moved = mouse(.mouseMoved, place, clicks: 0) { web.mouseMoved(with: moved) }
        if let down = mouse(.leftMouseDown, place, clicks: 1) { web.mouseDown(with: down) }
        if let up = mouse(.leftMouseUp, place, clicks: 1) { web.mouseUp(with: up) }
    }

    static let keys: [String: (code: UInt16, text: String, flags: NSEvent.ModifierFlags)] = [
        "Enter": (36, "\r", []), "Escape": (53, "\u{1B}", []), " ": (49, " ", []), "Tab": (48, "\t", []),
        "ArrowDown": (125, "\u{F701}", [.function, .numericPad]), "ArrowUp": (126, "\u{F700}", [.function, .numericPad]),
        "Home": (115, "\u{F729}", [.function]), "End": (119, "\u{F72B}", [.function]),
    ]

    func press(_ name: String) -> Bool {
        guard let key = Host.keys[name] else { return false }
        for type in [NSEvent.EventType.keyDown, .keyUp] {
            guard let event = NSEvent.keyEvent(with: type, location: .zero, modifierFlags: key.flags,
                                               timestamp: ProcessInfo.processInfo.systemUptime,
                                               windowNumber: window.windowNumber, context: nil, characters: key.text,
                                               charactersIgnoringModifiers: key.text, isARepeat: false,
                                               keyCode: key.code) else { return false }
            if type == .keyDown { web.keyDown(with: event) } else { web.keyUp(with: event) }
        }
        return true
    }

    // ---- a picture of the page as it is drawn ----
    func shot(_ path: String, scale: Double) {
        let configuration = WKSnapshotConfiguration()
        configuration.rect = web.bounds
        web.takeSnapshot(with: configuration) { image, error in
            guard let image = image else { return answer(["error": error?.localizedDescription ?? "no picture"]) }
            let size = image.size
            let wide = Int((Double(size.width) * scale).rounded()), high = Int((Double(size.height) * scale).rounded())
            guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: wide, pixelsHigh: high, bitsPerSample: 8,
                                             samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                             colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
                  let context = NSGraphicsContext(bitmapImageRep: rep) else { return answer(["error": "no bitmap"]) }
            rep.size = size
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = context
            image.draw(in: NSRect(origin: .zero, size: size))
            NSGraphicsContext.restoreGraphicsState()
            guard let data = rep.representation(using: .png, properties: [:]) else { return answer(["error": "no PNG"]) }
            do {
                try data.write(to: URL(fileURLWithPath: path))
                answer(["ok": true, "width": wide, "height": high])
            } catch { answer(["error": error.localizedDescription]) }
        }
    }

    func later(_ seconds: Double, _ work: @escaping () -> Void) {
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
    }

    func handle(_ line: String) {
        guard let command = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any],
              let op = command["op"] as? String else { return answer(["error": "not a command: \(line.prefix(80))"]) }
        let number = { (name: String) in (command[name] as? NSNumber)?.doubleValue ?? 0 }
        switch op {
        case "load":
            guard let url = URL(string: command["url"] as? String ?? "") else { return answer(["error": "not a URL"]) }
            loads += 1
            let mine = loads
            committed = { error in answer(error.map { ["error": $0] } ?? ["ok": true]) }
            web.load(URLRequest(url: url))
            later(10) { [weak self] in
                if self?.loads == mine { self?.settle("the page did not start to load") }
            }
        case "js":
            let body = "const value = await (\(command["expression"] as? String ?? "null")\n);"
                + " return JSON.stringify(value === undefined ? null : value);"
            web.callAsyncJavaScript(body, arguments: [:], in: nil, in: .page) { result in
                switch result {
                case .success(let value): answer(["value": value as? String ?? "null"])
                case .failure(let error):
                    let details = (error as NSError).userInfo
                    answer(["error": details["WKJavaScriptExceptionMessage"] as? String ?? error.localizedDescription])
                }
            }
        case "click":
            click(number("x"), number("y"))
            later(0.02) { answer(["ok": true]) }
        case "press":
            let pressed = press(command["key"] as? String ?? "")
            later(0.02) { answer(pressed ? ["ok": true] : ["error": "no such key"]) }
        case "size":
            window.setContentSize(NSSize(width: number("width"), height: number("height")))
            later(0.25) { answer(["ok": true]) }
        case "dark":
            window.appearance = NSAppearance(named: (command["on"] as? Bool ?? false) ? .darkAqua : .aqua)
            later(0.25) { answer(["ok": true]) }
        case "shot":
            shot(command["path"] as? String ?? "", scale: command["scale"] == nil ? 1 : number("scale"))
        case "problems":
            answer(["problems": problems])
            problems.removeAll()
        default:
            answer(["error": "no such command: \(op)"])
        }
    }
}

// ---- diff: two pictures, the pixels that differ, and the differing bands written a over b ----
func pixels(_ path: String) -> (data: [UInt8], width: Int, height: Int)? {
    guard let image = NSImage(contentsOfFile: path),
          let picture = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return nil }
    var data = [UInt8](repeating: 0, count: picture.width * picture.height * 4)
    guard let context = CGContext(data: &data, width: picture.width, height: picture.height, bitsPerComponent: 8,
                                  bytesPerRow: picture.width * 4, space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
    context.draw(picture, in: CGRect(x: 0, y: 0, width: picture.width, height: picture.height))
    return (data, picture.width, picture.height)
}

func diff(_ first: String, _ second: String, _ out: String) -> Int32 {
    guard let a = pixels(first), let b = pixels(second) else {
        answer(["error": "could not read both pictures"])
        return 2
    }
    let width = max(a.width, b.width), height = max(a.height, b.height)
    var rows = [Bool](repeating: false, count: height)
    var count = 0
    for y in 0..<height {
        for x in 0..<width {
            var same = x < a.width && x < b.width && y < a.height && y < b.height
            if same {
                let i = (y * a.width + x) * 4, j = (y * b.width + x) * 4
                for channel in 0..<4 where abs(Int(a.data[i + channel]) - Int(b.data[j + channel])) > 12 { same = false }
            }
            if !same {
                count += 1
                rows[y] = true
            }
        }
    }
    // Rows that differ, joined when fewer than 24 equal rows lie between them, with 16 rows of margin.
    var bands: [[Int]] = []
    var y = 0
    while y < height {
        if rows[y] {
            var end = y
            var gap = 0
            var next = y + 1
            while next < height && gap < 24 {
                if rows[next] { end = next; gap = 0 } else { gap += 1 }
                next += 1
            }
            bands.append([max(0, y - 16), min(height - 1, end + 16)])
            y = end + 1
        } else { y += 1 }
    }
    var report: [String: Any] = ["different": count, "a": [a.width, a.height], "b": [b.width, b.height], "bands": bands]
    if count > 0 {
        let total = bands.reduce(0) { $0 + ($1[1] - $1[0] + 1) * 2 + 10 }
        if let context = CGContext(data: nil, width: width, height: total, bitsPerComponent: 8, bytesPerRow: width * 4,
                                   space: CGColorSpaceCreateDeviceRGB(),
                                   bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) {
            context.setFillColor(CGColor(red: 1, green: 0, blue: 1, alpha: 1))
            context.fill(CGRect(x: 0, y: 0, width: width, height: total))
            var top = 0                                    // from the top of the output picture
            for band in bands {
                let tall = band[1] - band[0] + 1
                for (side, path) in [first, second].enumerated() {
                    guard let image = NSImage(contentsOfFile: path),
                          let picture = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
                    let crop = CGRect(x: 0, y: band[0], width: picture.width, height: min(tall, max(0, picture.height - band[0])))
                    if crop.height > 0, let piece = picture.cropping(to: crop) {
                        let below = top + side * (tall + 2)
                        context.draw(piece, in: CGRect(x: 0, y: total - below - Int(crop.height), width: piece.width,
                                                       height: Int(crop.height)))
                    }
                }
                top += tall * 2 + 10
            }
            if let made = context.makeImage(),
               let data = NSBitmapImageRep(cgImage: made).representation(using: .png, properties: [:]) {
                try? data.write(to: URL(fileURLWithPath: out))
                report["out"] = out
            }
        }
    }
    answer(report)
    return count == 0 ? 0 : 1
}

let arguments = CommandLine.arguments
if arguments.count == 5 && arguments[1] == "diff" {
    exit(diff(arguments[2], arguments[3], arguments[4]))
}
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let host = Host(agent: arguments.count > 1 && arguments[1] == "browser" ? browserAgent : appAgent)
Thread.detachNewThread {
    while let line = readLine() {
        DispatchQueue.main.async { host.handle(line) }
    }
    exit(0)                                     // the tests ended or died: so does this
}
application.run()
