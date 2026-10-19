import AppKit
import WebKit
import UniformTypeIdentifiers

// The native window owns exactly one local engine process. It never uses a
// machine-wide Python installation, opens a Terminal window, or exposes LAN access.
final class FilmDustApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var engine: Process?
    var outputPipe: Pipe?
    var endpoint: URL?
    var startupText = ""
    var terminating = false
    var loaded = false
    var allowTermination = false
    var verified = false
    var verificationInput: URL?
    var verificationPreferences: [String: Any]?
    var verificationRepeat = false
    var verificationPass = 0
    var verificationRuns: [[String: Any]] = []
    var verificationBrush: [Double]?
    var verificationDidBrush = false
    var verifiedCloseGuard = false
    var preferencesStore: UserDefaults {
        verificationDirectory == nil ? .standard : UserDefaults(suiteName: "local.filmdust.verification")!
    }
    var verificationDirectory: URL?
    var verificationTimer: Timer?
    var verificationDeadline = Date.distantFuture
    let network: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.connectionProxyDictionary = [:]
        configuration.timeoutIntervalForRequest = 90
        return URLSession(configuration: configuration)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--verify-app"), args.count > i + 1 {
            verificationDirectory = URL(fileURLWithPath: args[i + 1], isDirectory: true)
        }
        if let i = args.firstIndex(of: "--verify-input"), args.count > i + 1 {
            verificationInput = URL(fileURLWithPath: args[i + 1])
        }
        if let i = args.firstIndex(of: "--verify-preferences"), args.count > i + 1,
           let data = args[i + 1].data(using: .utf8) {
            verificationPreferences = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        }
        verificationRepeat = args.contains("--verify-repeat")
        if let i = args.firstIndex(of: "--verify-brush"), args.count > i + 1 {
            let values = args[i + 1].split(separator: ",").compactMap { Double($0) }
            if values.count == 3 && values.allSatisfy({ $0.isFinite }) { verificationBrush = values }
        }
        setupMenus()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        for name in ["filmExport", "filmPreferences", "filmClose"] {
            configuration.userContentController.add(self, name: name)
        }
        let preferences = verificationPreferences ?? preferencesStore.dictionary(forKey: "cleanupPreferences.v1") ?? [:]
        if let data = try? JSONSerialization.data(withJSONObject: preferences), let json = String(data: data, encoding: .utf8) {
            configuration.userContentController.addUserScript(WKUserScript(source: "window.filmDustPreferences = \(json);", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
        configuration.limitsNavigationsToAppBoundDomains = true
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "FilmDust · 胶片除尘"
        window.minSize = NSSize(width: 640, height: 620)
        window.delegate = self
        window.contentView = webView
        window.isReleasedWhenClosed = false
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startEngine()
    }

    func setupMenus() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        let about = NSMenuItem(title: "关于 FilmDust", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(about); appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 FilmDust", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 FilmDust", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let fileItem = NSMenuItem(title: "文件", action: nil, keyEquivalent: ""); main.addItem(fileItem)
        let fileMenu = NSMenu(title: "文件"); fileItem.submenu = fileMenu
        let open = NSMenuItem(title: "导入照片…", action: #selector(importPhoto), keyEquivalent: "o"); open.target = self; fileMenu.addItem(open)
        let save = NSMenuItem(title: "保存副本…", action: #selector(savePhoto), keyEquivalent: "s"); save.target = self; fileMenu.addItem(save)
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: ""); main.addItem(editItem)
        let editMenu = NSMenu(title: "编辑"); editItem.submenu = editMenu
        let undo = NSMenuItem(title: "撤销", action: #selector(undoEdit), keyEquivalent: "z"); undo.target = self; editMenu.addItem(undo)
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        NSApp.mainMenu = main
    }

    @objc func importPhoto() { if loaded { webView.evaluateJavaScript("document.getElementById('fileInput').click()", completionHandler: nil) } }
    @objc func savePhoto() { if loaded { webView.evaluateJavaScript("document.getElementById('exportBtn').click()", completionHandler: nil) } }
    @objc func undoEdit() { if loaded { webView.evaluateJavaScript("document.getElementById('undoBtn').click()", completionHandler: nil) } }

    func startEngine() {
        guard let resources = Bundle.main.resourceURL else { fail("应用文件不完整，请重新解压 FilmDust。"); return }
        let executable = resources.appendingPathComponent("Engine/FilmDustEngine")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else { fail("除尘组件缺失，请重新解压 FilmDust。"); return }
        let process = Process()
        process.executableURL = executable
        process.arguments = ["--port", "0", "--no-browser"]
        process.currentDirectoryURL = resources.appendingPathComponent("Engine")
        var environment = ProcessInfo.processInfo.environment
        environment.removeValue(forKey: "PYTHONPATH")
        environment.removeValue(forKey: "PYTHONHOME")
        environment.removeValue(forKey: "VIRTUAL_ENV")
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        let pipe = Pipe(); outputPipe = pipe
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.engineOutput(text) }
        }
        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self = self, !self.terminating else { return }
                self.fail("除尘组件已停止（\(process.terminationStatus)）。重新打开 App 后再试。")
            }
        }
        engine = process
        do { try process.run() } catch { fail("除尘组件无法启动：\(error.localizedDescription)"); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in
            guard let self = self, self.endpoint == nil, !self.terminating else { return }
            self.fail("启动超时，请退出后重新打开 FilmDust。")
        }
    }

    func engineOutput(_ text: String) {
        guard endpoint == nil else { return }
        startupText += text
        if startupText.count > 8192 { startupText = String(startupText.suffix(8192)) }
        guard let range = startupText.range(of: "http://127\\.0\\.0\\.1:[0-9]+", options: .regularExpression), let url = URL(string: String(startupText[range])) else { return }
        endpoint = url
        startupText = ""
        webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loaded = true
        if verificationDirectory != nil { beginVerification() }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { fail("界面加载失败：\(error.localizedDescription)") }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { fail("界面加载失败：\(error.localizedDescription)") }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "http", url.host == "127.0.0.1", url.port == endpoint?.port { decisionHandler(.allow) }
        else { decisionHandler(.cancel) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.title = "导入扫描照片"
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.jpeg, .png, .tiff]
        panel.beginSheetModal(for: window) { response in completionHandler(response == .OK ? panel.urls : nil) }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame,
              message.frameInfo.securityOrigin.protocol == "http", message.frameInfo.securityOrigin.host == "127.0.0.1",
              message.frameInfo.securityOrigin.port == endpoint?.port,
              let body = message.body as? [String: Any] else { return }
        if message.name == "filmPreferences" {
            guard let sensitivity = body["sensitivity"] as? Double, (1...100).contains(sensitivity),
                  let size = body["size"] as? Double, (4...100).contains(size),
                  let polarity = body["polarity"] as? String, ["light", "dark", "both"].contains(polarity),
                  let protect = body["protect"] as? Bool, let hairs = body["hairs"] as? Bool else { return }
            preferencesStore.set(["sensitivity": sensitivity, "size": size, "polarity": polarity, "protect": protect, "hairs": hairs], forKey: "cleanupPreferences.v1")
            return
        }
        if message.name == "filmClose", body["confirmed"] as? Bool == true {
            allowTermination = true
            NSApp.terminate(nil)
            return
        }
        guard message.name == "filmExport", let requestId = body["requestId"] as? String,
              let revision = body["revision"] as? Int,
              let id = body["id"] as? String, let format = body["format"] as? String,
              let token = body["token"] as? String, let name = body["name"] as? String,
              ["tiff", "png", "jpeg"].contains(format), id.count <= 256, token.count <= 256, requestId.count <= 128 else { return }
        if let directory = verificationDirectory {
            export(id: id, revision: revision, format: format, token: token, requestId: requestId, destination: directory.appendingPathComponent("native-export.tif"))
            return
        }
        let panel = NSSavePanel()
        panel.title = "保存除尘副本"
        panel.nameFieldStringValue = URL(fileURLWithPath: name).lastPathComponent
        panel.allowedContentTypes = [format == "tiff" ? .tiff : format == "png" ? .png : .jpeg]
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self = self else { return }
            guard response == .OK, let destination = panel.url else { self.complete(["requestId": requestId, "cancelled": true]); return }
            self.export(id: id, revision: revision, format: format, token: token, requestId: requestId, destination: destination)
        }
    }

    func export(id: String, revision: Int, format: String, token: String, requestId: String, destination: URL) {
        guard let endpoint = endpoint else { complete(["requestId": requestId, "error": "本机服务未连接，请重新打开 App。"]); return }
        var request = URLRequest(url: endpoint.appendingPathComponent("api/export"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(token, forHTTPHeaderField: "X-FilmDust-Token")
        request.setValue(endpoint.absoluteString, forHTTPHeaderField: "Origin")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["id": id, "revision": revision, "format": format])
        network.dataTask(with: request) { [weak self] data, response, error in
            var message: [String: Any] = ["requestId": requestId]
            if let error = error { message["error"] = error.localizedDescription }
            else if let response = response as? HTTPURLResponse, response.statusCode == 200, let data = data {
                do { try data.write(to: destination, options: .atomic); message["path"] = destination.path }
                catch { message["error"] = "保存失败：\(error.localizedDescription)" }
            } else {
                let detail = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                message["error"] = (detail?["error"] as? String) ?? "副本生成失败，请重试。"
            }
            DispatchQueue.main.async { self?.complete(message) }
        }.resume()
    }

    func complete(_ message: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: message), let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.filmDustNativeExportComplete(\(json))") { [weak self] _, _ in
            guard let self = self else { return }
            if self.verificationDirectory != nil, message["path"] != nil { self.recordVerification() }
        }
        if verificationDirectory != nil, let error = message["error"] as? String { verificationFailure(error) }
    }

    // A deterministic, opt-in integration check for this app's own bundled page,
    // process and native save bridge. External inputs are opt-in, never bundled.
    func beginVerification() {
        guard verificationTimer == nil, !verified, let directory = verificationDirectory else { return }
        do { try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true) }
        catch { verificationFailure(error.localizedDescription); return }
        verificationDeadline = Date().addingTimeInterval(60)
        if verificationPreferences != nil {
            webView.evaluateJavaScript("storePreferences()", completionHandler: nil)
        }
        if let input = verificationInput {
            do {
                let data = try Data(contentsOf: input)
                let names = try JSONSerialization.data(withJSONObject: [input.lastPathComponent])
                let nameJSON = String(data: names, encoding: .utf8)!
                let script = "importPhoto(new File([Uint8Array.from(atob('\(data.base64EncodedString())'),c=>c.charCodeAt(0))],\(nameJSON)[0]))"
                webView.evaluateJavaScript(script, completionHandler: nil)
            } catch { verificationFailure(error.localizedDescription); return }
        } else {
            webView.evaluateJavaScript("document.getElementById('sampleBtn').click()", completionHandler: nil)
        }
        verificationTimer = Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            if Date() > self.verificationDeadline { self.verificationFailure("Bundled page processing timed out"); return }
            let waitForBrush = self.verificationDidBrush ? " && !!window.filmDustBrushCheck" : ""
            self.webView.evaluateJavaScript("!state.busy && state.ready && (state.lastDuration !== null || state.lastEditDuration !== null)\(waitForBrush)") { value, error in
                if let error = error { self.verificationFailure(error.localizedDescription); return }
                guard (value as? Bool) == true else { return }
                if let brush = self.verificationBrush, !self.verificationDidBrush {
                    self.verificationDidBrush = true
                    let script = """
                    (async()=>{
                      try {
                        const point=[\(brush[0]),\(brush[1])], diameter=\(brush[2]), autoSeconds=state.lastDuration;
                        const pixel=()=>{const c=document.createElement('canvas');c.width=c.height=1;const t=c.getContext('2d');t.drawImage(state.result,point[0],point[1],1,1,0,0,1,1);return Array.from(t.getImageData(0,0,1,1).data);};
                        const before=pixel();
                        await mutation('/api/edit',{mode:'add',points:[point],diameter},'正在修补这一处…');
                        const after=pixel(), firstSeconds=state.lastEditDuration, patch=state.info.preview_patch;
                        await mutation('/api/undo',{},'正在撤销这一处…');
                        const undo=pixel(),undoSeconds=state.lastEditDuration;
                        if(JSON.stringify(before)!==JSON.stringify(undo))throw Error('Undo pixels differ');
                        await mutation('/api/edit',{mode:'add',points:[point],diameter},'正在修补这一处…');
                        window.filmDustBrushCheck={before,after,undo,firstSeconds,undoSeconds,repeatSeconds:state.lastEditDuration,patch,autoSeconds,changedPixels:state.info.edit.changed_pixels};
                      }catch(e){window.filmDustBrushCheck={error:String(e)};}
                    })()
                    """
                    self.webView.evaluateJavaScript(script, completionHandler: nil)
                    return
                }
                self.verificationTimer?.invalidate(); self.verificationTimer = nil
                if !self.verifiedCloseGuard {
                    self.window.performClose(nil)
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                        self.webView.evaluateJavaScript("(()=>{const d=document.getElementById('unsavedDialog');const shown=d.open;if(shown)d.close('cancel');return shown&&needsSave();})()") { value, _ in
                            guard value as? Bool == true else { self.verificationFailure("Unsaved close guard did not preserve the photo"); return }
                            self.verifiedCloseGuard = true
                            self.webView.evaluateJavaScript("document.getElementById('exportBtn').click()", completionHandler: nil)
                        }
                    }
                } else {
                    self.webView.evaluateJavaScript("document.getElementById('exportBtn').click()", completionHandler: nil)
                }
            }
        }
    }

    func recordVerification() {
        webView.evaluateJavaScript("JSON.stringify({seconds:window.filmDustBrushCheck?.autoSeconds??state.lastDuration,brush:window.filmDustBrushCheck,settings:state.info.detection.settings,revision:state.info.revision,width:state.info.width,height:state.info.height,processed:state.info.processed_count,dirty:needsSave()})") { [weak self] value, _ in
            guard let self = self else { return }
            if let value = value as? String, let data = value.data(using: .utf8), let report = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] { self.verificationRuns.append(report) }
            if self.verificationRepeat && self.verificationPass == 0 {
                self.verificationPass += 1
                self.beginVerification()
            } else { self.finishVerification() }
        }
    }

    func finishVerification() {
        guard !verified, let directory = verificationDirectory else { return }
        verified = true
        if let data = try? JSONSerialization.data(withJSONObject: verificationRuns, options: [.prettyPrinted, .sortedKeys]) { try? data.write(to: directory.appendingPathComponent("native-runs.json")) }
        webView.evaluateJavaScript("JSON.stringify({ready:!document.getElementById('exportBtn').disabled,summary:document.getElementById('resultSummary').textContent,width:innerWidth,height:innerHeight,nativeBridge:!!window.webkit.messageHandlers.filmExport,closeGuard:\(verifiedCloseGuard),port:location.port})") { [weak self] value, error in
            guard let self = self else { return }
            if let value = value as? String { try? value.write(to: directory.appendingPathComponent("native-check.json"), atomically: true, encoding: .utf8) }
            let config = WKSnapshotConfiguration()
            self.webView.takeSnapshot(with: config) { image, error in
                if let image = image, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff), let data = rep.representation(using: .png, properties: [:]) {
                    try? data.write(to: directory.appendingPathComponent("native-window.png"))
                }
                self.terminating = true
                self.stopEngine()
                NSApp.terminate(nil)
            }
        }
    }

    func verificationFailure(_ text: String) {
        verificationTimer?.invalidate(); verificationTimer = nil
        guard let directory = verificationDirectory else { terminating = true; stopEngine(); NSApp.terminate(nil); return }
        try? text.write(to: directory.appendingPathComponent("native-failure.txt"), atomically: true, encoding: .utf8)
        webView.evaluateJavaScript("JSON.stringify({busy:state.busy,ready:state.ready,revision:state.info?.revision,renderedRevision:state.renderedRevision,seconds:state.lastDuration,editSeconds:state.lastEditDuration,brush:window.filmDustBrushCheck,toast:document.getElementById('toast').textContent,phase:document.getElementById('busyText').textContent})") { [weak self] value, _ in
            guard let self = self else { return }
            if let value = value as? String { try? value.write(to: directory.appendingPathComponent("native-failure-state.json"), atomically: true, encoding: .utf8) }
            self.webView.takeSnapshot(with: WKSnapshotConfiguration()) { image, _ in
                if let image = image, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff), let data = rep.representation(using: .png, properties: [:]) { try? data.write(to: directory.appendingPathComponent("native-failure-window.png")) }
                self.terminating = true; self.stopEngine(); NSApp.terminate(nil)
            }
        }
    }

    func fail(_ text: String) {
        if verificationDirectory != nil { verificationFailure(text); return }
        let alert = NSAlert(); alert.messageText = "FilmDust 未能启动"; alert.informativeText = text
        alert.addButton(withTitle: "退出")
        alert.runModal()
        terminating = true; stopEngine(); NSApp.terminate(nil)
    }

    func stopEngine() {
        verificationTimer?.invalidate(); verificationTimer = nil
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        if let engine = engine, engine.isRunning { engine.terminate() }
        network.invalidateAndCancel()
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if allowTermination || terminating || !loaded { return true }
        webView.evaluateJavaScript("window.filmDustRequestClose()", completionHandler: nil)
        return false
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if allowTermination || terminating || !loaded { return .terminateNow }
        webView.evaluateJavaScript("window.filmDustRequestClose()", completionHandler: nil)
        return .terminateCancel
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { terminating = true; stopEngine() }
}

let application = NSApplication.shared
let delegate = FilmDustApp()
application.setActivationPolicy(.regular)
application.delegate = delegate
application.run()
