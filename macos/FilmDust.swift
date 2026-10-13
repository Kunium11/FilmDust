import AppKit
import WebKit
import UniformTypeIdentifiers

// The native window owns exactly one local engine process. It never uses a
// machine-wide Python installation, opens a Terminal window, or exposes LAN access.
final class FilmDustApp: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var engine: Process?
    var outputPipe: Pipe?
    var endpoint: URL?
    var startupText = ""
    var terminating = false
    var loaded = false
    var verified = false
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
        setupMenus()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.userContentController.add(self, name: "filmExport")
        configuration.limitsNavigationsToAppBoundDomains = true
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "FilmDust · 胶片除尘"
        window.minSize = NSSize(width: 640, height: 620)
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
        guard message.name == "filmExport", message.frameInfo.isMainFrame,
              message.frameInfo.securityOrigin.protocol == "http", message.frameInfo.securityOrigin.host == "127.0.0.1",
              message.frameInfo.securityOrigin.port == endpoint?.port,
              let body = message.body as? [String: Any], let requestId = body["requestId"] as? String,
              let id = body["id"] as? String, let format = body["format"] as? String,
              let token = body["token"] as? String, let name = body["name"] as? String,
              ["tiff", "png", "jpeg"].contains(format), id.count <= 256, token.count <= 256, requestId.count <= 128 else { return }
        if let directory = verificationDirectory {
            export(id: id, format: format, token: token, requestId: requestId, destination: directory.appendingPathComponent("native-export.tif"))
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
            self.export(id: id, format: format, token: token, requestId: requestId, destination: destination)
        }
    }

    func export(id: String, format: String, token: String, requestId: String, destination: URL) {
        guard let endpoint = endpoint else { complete(["requestId": requestId, "error": "本机服务未连接，请重新打开 App。"]); return }
        var request = URLRequest(url: endpoint.appendingPathComponent("api/export"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(token, forHTTPHeaderField: "X-FilmDust-Token")
        request.setValue(endpoint.absoluteString, forHTTPHeaderField: "Origin")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["id": id, "format": format])
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
        webView.evaluateJavaScript("window.filmDustNativeExportComplete(\(json))", completionHandler: nil)
        if verificationDirectory != nil, message["path"] != nil { finishVerification() }
        if verificationDirectory != nil, let error = message["error"] as? String { verificationFailure(error) }
    }

    // A deterministic, opt-in integration check for this app's own bundled page,
    // process and native save bridge. It uses only the bundled sample photo.
    func beginVerification() {
        guard verificationTimer == nil, !verified, let directory = verificationDirectory else { return }
        do { try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true) }
        catch { verificationFailure(error.localizedDescription); return }
        verificationDeadline = Date().addingTimeInterval(60)
        webView.evaluateJavaScript("document.getElementById('sampleBtn').click()", completionHandler: nil)
        verificationTimer = Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            if Date() > self.verificationDeadline { self.verificationFailure("Bundled page processing timed out"); return }
            self.webView.evaluateJavaScript("!document.getElementById('exportBtn').disabled && document.getElementById('resultSummary').textContent.includes('已处理')") { value, error in
                if let error = error { self.verificationFailure(error.localizedDescription); return }
                guard (value as? Bool) == true else { return }
                self.verificationTimer?.invalidate(); self.verificationTimer = nil
                self.webView.evaluateJavaScript("document.getElementById('exportBtn').click()", completionHandler: nil)
            }
        }
    }

    func finishVerification() {
        guard !verified, let directory = verificationDirectory else { return }
        verified = true
        webView.evaluateJavaScript("JSON.stringify({ready:!document.getElementById('exportBtn').disabled,summary:document.getElementById('resultSummary').textContent,width:innerWidth,height:innerHeight,nativeBridge:!!window.webkit.messageHandlers.filmExport})") { [weak self] value, error in
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
        if let directory = verificationDirectory { try? text.write(to: directory.appendingPathComponent("native-failure.txt"), atomically: true, encoding: .utf8) }
        terminating = true; stopEngine(); NSApp.terminate(nil)
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
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { terminating = true; stopEngine() }
}

let application = NSApplication.shared
let delegate = FilmDustApp()
application.setActivationPolicy(.regular)
application.delegate = delegate
application.run()
