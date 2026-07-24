import AppKit
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backend: Process?
    private var frontend: Process?
    private var readinessTimer: Timer?
    private var attempts = 0

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        createApplicationMenu()
        createWindow()
        do {
            try prepareDataDirectory()
            try startServices()
            waitForFrontend()
        } catch {
            showError("应用启动失败：\(error.localizedDescription)")
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        readinessTimer?.invalidate()
        terminate(frontend)
        terminate(backend)
    }

    private func createWindow() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "中建探员"
        window.titlebarAppearsTransparent = true
        window.minSize = NSSize(width: 1080, height: 700)
        window.center()
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
        showLoading()
    }

    private func createApplicationMenu() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "退出中建探员",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        appMenuItem.submenu = appMenu
        NSApp.mainMenu = mainMenu
    }

    private var resources: URL {
        Bundle.main.resourceURL!
    }

    private var appData: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/中建探员", isDirectory: true)
    }

    private func prepareDataDirectory() throws {
        let manager = FileManager.default
        try manager.createDirectory(at: appData, withIntermediateDirectories: true)
        try manager.createDirectory(at: appData.appendingPathComponent("output", isDirectory: true), withIntermediateDirectories: true)
        try manager.createDirectory(at: appData.appendingPathComponent("logs", isDirectory: true), withIntermediateDirectories: true)

        let envPath = appData.appendingPathComponent(".env.local")
        if !manager.fileExists(atPath: envPath.path) {
            try manager.copyItem(at: resources.appendingPathComponent("defaults/.env.example"), to: envPath)
        }
        let companiesPath = appData.appendingPathComponent("monitor_companies.txt")
        if !manager.fileExists(atPath: companiesPath.path) {
            try manager.copyItem(at: resources.appendingPathComponent("defaults/monitor_companies.txt"), to: companiesPath)
        }
    }

    private func startServices() throws {
        var environment = ProcessInfo.processInfo.environment
        environment["CREDITCHINA_ENV_PATH"] = appData.appendingPathComponent(".env.local").path
        environment["CREDITCHINA_OUTPUT"] = appData.appendingPathComponent("output").path
        environment["CREDITCHINA_API_STATE"] = appData.appendingPathComponent("output/creditchina_api.sqlite3").path
        environment["CREDITCHINA_MONITOR_COMPANIES"] = appData.appendingPathComponent("monitor_companies.txt").path
        environment["PYTHONUNBUFFERED"] = "1"

        backend = try launch(
            executable: resources.appendingPathComponent("backend/zhongjian-agent-backend"),
            arguments: ["--host", "127.0.0.1", "--port", "8765"],
            environment: environment,
            logName: "backend.log"
        )
        frontend = try launch(
            executable: resources.appendingPathComponent("runtime/node"),
            arguments: [resources.appendingPathComponent("packaging/frontend-server.mjs").path, "3000"],
            environment: environment,
            logName: "frontend.log",
            currentDirectory: resources
        )
    }

    private func launch(
        executable: URL,
        arguments: [String],
        environment: [String: String],
        logName: String,
        currentDirectory: URL? = nil
    ) throws -> Process {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.environment = environment
        process.currentDirectoryURL = currentDirectory
        let logURL = appData.appendingPathComponent("logs/\(logName)")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: logURL)
        try handle.seekToEnd()
        process.standardOutput = handle
        process.standardError = handle
        try process.run()
        return process
    }

    private func waitForFrontend() {
        readinessTimer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] _ in
            self?.probeFrontend()
        }
        readinessTimer?.fire()
    }

    private func probeFrontend() {
        guard let url = URL(string: "http://127.0.0.1:3000/") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 0.35
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            guard let self, response != nil else {
                DispatchQueue.main.async {
                    self?.attempts += 1
                    if self?.attempts == 50 { self?.showError("页面服务启动超时，请重新打开应用。") }
                }
                return
            }
            DispatchQueue.main.async {
                self.readinessTimer?.invalidate()
                self.webView.load(URLRequest(url: url))
            }
        }.resume()
    }

    private func showLoading() {
        webView.loadHTMLString("""
        <html><meta charset="utf-8"><style>
        body{margin:0;height:100vh;display:grid;place-items:center;background:#f5f7fb;color:#101828;font:15px -apple-system}
        div{text-align:center}b{display:block;font-size:24px;margin-bottom:10px}span{color:#98a2b3}
        </style><body><div><b>中建探员</b><span>信息中国信息采集 · 正在启动…</span></div></body></html>
        """, baseURL: nil)
    }

    private func showError(_ message: String) {
        let escaped = message.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        webView.loadHTMLString("<html><meta charset='utf-8'><body style='font:15px -apple-system;padding:50px'><h2>中建探员</h2><p>\(escaped)</p></body></html>", baseURL: nil)
    }

    private func terminate(_ process: Process?) {
        guard let process, process.isRunning else { return }
        process.terminate()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
