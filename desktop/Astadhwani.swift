import AppKit
import WebKit
import Foundation

final class StudioDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var engine: Process?
    var output = Data()
    var localOrigin: String?
    var logHandle: FileHandle?
    var startupTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let appMenu = NSMenu()
        let item = NSMenuItem(); appMenu.addItem(item)
        let actions = NSMenu(); actions.addItem(withTitle: "About Astadhwani", action: #selector(about), keyEquivalent: "")
        actions.addItem(NSMenuItem.separator()); actions.addItem(withTitle: "Quit Astadhwani", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"); item.submenu = actions
        let edit = NSMenuItem(); appMenu.addItem(edit); edit.title = "Edit"
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"); edit.submenu = editMenu
        NSApp.mainMenu = appMenu
        window = NSWindow(contentRect: NSRect(x:0,y:0,width:1230,height:860), styleMask:[.titled,.closable,.miniaturizable,.resizable], backing:.buffered, defer:false)
        window.title = "Astadhwani Studio"; window.minSize = NSSize(width:720,height:600); window.center()
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        webView = WKWebView(frame:window.contentView!.bounds, configuration:config)
        webView.autoresizingMask = [.width,.height]; webView.navigationDelegate = self; webView.uiDelegate = self
        window.contentView = webView
        webView.loadHTMLString("<html><body style='background:#f7f5ef;color:#526345;font:18px -apple-system;text-align:center;padding-top:27vh'><h1 style='font:40px Georgia'>Astadhwani</h1><p>Opening your local studio…</p><small>No cloud connection needed.</small></body></html>", baseURL:nil)
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps:true)
        startEngine()
    }
    @objc func about() {
        let alert = NSAlert(); alert.messageText = "Astadhwani Studio"
        alert.informativeText = "Nepali speech and reference voices\nApple Silicon · On-device generation\nYour text and audio stay on your Mac.\n\nAstadhwani by Ampixa Labs."
        alert.runModal()
    }
    func startEngine() {
        guard let resources = Bundle.main.resourceURL else { fail("App resources are missing."); return }
        let support = FileManager.default.urls(for:.applicationSupportDirectory,in:.userDomainMask)[0].appendingPathComponent("Astadhwani/Studio",isDirectory:true)
        if let data = try? Data(contentsOf: support.appendingPathComponent(".engine.json")),
           let info = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let origin = info["origin"] as? String, let instance = info["instance_id"] as? String,
           let url = URL(string: origin), url.scheme == "http", url.host == "127.0.0.1", url.port != nil,
           let health = URL(string: origin + "/api/health") {
            var request = URLRequest(url: health); request.timeoutInterval = 2
            URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    if let data = data, let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                       json["app"] as? String == "astadhwani", json["instance_id"] as? String == instance {
                        self.localOrigin = origin
                        self.openWhenReady(origin, attempt: 0)
                    } else { self.launchEngine(resources, support) }
                }
            }.resume()
            return
        }
        launchEngine(resources, support)
    }
    func launchEngine(_ resources: URL, _ support: URL) {
        do {
            try FileManager.default.createDirectory(at:support,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
            let logURL = support.appendingPathComponent("engine.log")
            FileManager.default.createFile(atPath:logURL.path,contents:nil,attributes:[.posixPermissions:0o600])
            logHandle = try FileHandle(forWritingTo:logURL)
            let p = Process(); engine = p
            p.executableURL = resources.appendingPathComponent("python/bin/python3.11")
            p.arguments = [resources.appendingPathComponent("studio/backend/server.py").path,"--data",support.path,"--model",resources.appendingPathComponent("models/wave1").path,"--web",resources.appendingPathComponent("studio/web/dist").path]
            p.currentDirectoryURL = resources
            var env = ProcessInfo.processInfo.environment
            env.removeValue(forKey:"PYTHONHOME"); env.removeValue(forKey:"VIRTUAL_ENV")
            env["PYTHONPATH"] = resources.appendingPathComponent("site-packages").path
            env["PYTHONDONTWRITEBYTECODE"] = "1"; env["PYTHONUNBUFFERED"] = "1"
            env["OMP_NUM_THREADS"] = "2"; env["HF_HUB_OFFLINE"] = "1"; env["TRANSFORMERS_OFFLINE"] = "1"
            p.environment = env
            let pipe = Pipe(); p.standardOutput = pipe; p.standardError = logHandle
            pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let bytes = handle.availableData
                if bytes.isEmpty { handle.readabilityHandler = nil; return }
                DispatchQueue.main.async {
                    guard let self = self else { return }; self.output.append(bytes)
                    if let text = String(data:self.output,encoding:.utf8), let line = text.components(separatedBy:"\n").first(where:{$0.hasPrefix("ASTADHWANI_READY=")}), self.localOrigin == nil {
                        let origin = String(line.dropFirst("ASTADHWANI_READY=".count)); self.localOrigin = origin; self.openWhenReady(origin,attempt:0)
                    }
                }
            }
            p.terminationHandler = { [weak self] p in
                DispatchQueue.main.async {
                    guard let self = self, NSApp.isRunning else { return }
                    self.fail("The local engine stopped (code \(p.terminationStatus)). Reopen the app. Diagnostic log: \(logURL.path)")
                }
            }
            try p.run()
            startupTimer = Timer.scheduledTimer(withTimeInterval:45,repeats:false) { [weak self] _ in
                guard let self = self, self.localOrigin == nil else { return }; self.fail("The local engine did not start. Check engine.log in Application Support/Astadhwani/Studio.")
            }
        } catch { fail("Cannot start the local engine: \(error.localizedDescription)") }
    }
    func openWhenReady(_ origin:String, attempt:Int) {
        guard let url = URL(string:origin+"/api/health") else {return}
        URLSession.shared.dataTask(with:url) { [weak self] data,response,error in
            DispatchQueue.main.async {
                guard let self = self else {return}
                if (response as? HTTPURLResponse)?.statusCode == 200 {
                    self.startupTimer?.invalidate(); self.webView.load(URLRequest(url:URL(string:origin+"/")!))
                } else if attempt < 30 {
                    DispatchQueue.main.asyncAfter(deadline:.now()+0.2) {self.openWhenReady(origin,attempt:attempt+1)}
                } else {self.fail("The local studio is not responding. Please reopen the app.")}
            }
        }.resume()
    }
    func fail(_ message:String) {
        startupTimer?.invalidate()
        let alert = NSAlert(); alert.messageText = "Astadhwani needs attention"; alert.informativeText = message; alert.runModal()
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool {true}
    func applicationWillTerminate(_ notification:Notification) {
        engine?.terminationHandler = nil
        if engine?.isRunning == true {engine?.terminate()}
        try? logHandle?.close()
    }
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType, decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        guard let local = localOrigin, let url = URL(string: local), frame.isMainFrame,
              origin.protocol == "http", origin.host == "127.0.0.1", origin.port == url.port,
              type == .microphone else { decisionHandler(.deny); return }
        // Keep the normal WebKit/macOS user prompt; only the local main frame may ask for a microphone.
        decisionHandler(.prompt)
    }
    func webView(_ webView:WKWebView, decidePolicyFor action:WKNavigationAction, decisionHandler:@escaping(WKNavigationActionPolicy)->Void) {
        guard let url=action.request.url else {decisionHandler(.cancel);return}
        if url.scheme == "about" {decisionHandler(.allow);return}
        guard let origin=localOrigin, url.absoluteString.hasPrefix(origin+"/") else {decisionHandler(.cancel);return}
        decisionHandler(action.shouldPerformDownload ? .download : .allow)
    }
    func webView(_ webView:WKWebView,navigationAction:WKNavigationAction,didBecome download:WKDownload) {download.delegate=self}
    func webView(_ webView:WKWebView,navigationResponse:WKNavigationResponse,didBecome download:WKDownload) {download.delegate=self}
    func download(_ download:WKDownload,decideDestinationUsing response:URLResponse,suggestedFilename:String,completionHandler:@escaping(URL?)->Void) {
        let panel=NSSavePanel(); panel.nameFieldStringValue=suggestedFilename;panel.canCreateDirectories=true
        panel.beginSheetModal(for:window) {result in completionHandler(result == .OK ? panel.url : nil)}
    }
    func webView(_ webView:WKWebView,runJavaScriptConfirmPanelWithMessage message:String,initiatedByFrame frame:WKFrameInfo,completionHandler:@escaping(Bool)->Void) {
        let alert=NSAlert();alert.messageText="Astadhwani";alert.informativeText=message;alert.addButton(withTitle:"Continue");alert.addButton(withTitle:"Cancel");alert.beginSheetModal(for:window){completionHandler($0 == .alertFirstButtonReturn)}
    }
}
let app=NSApplication.shared
let delegate=StudioDelegate()
app.delegate=delegate
app.run()
