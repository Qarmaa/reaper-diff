// Reaper Diff: различия между двумя сессиями Reaper (.rpp) и сохранение различий в отдельную сессию.
// Окно над ptx_tools/rpp_diff.py: A (старая) + B (новая) → список различий → Save Diff Session.

import SwiftUI
import AppKit
import UniformTypeIdentifiers

// MARK: - Данные (JSON из rpp_diff.py --json)

struct DiffSide: Decodable {
    let pos: Double
    let length: Double
    let soffs: Double
    let track: String
    let name: String
    let file: String
}

struct ItemDiff: Decodable, Identifiable {
    let kind: String                 // added | removed | changed
    let tags: [String]
    let changes: [String]
    let pos: Double
    let track: String
    let name: String
    let file: String
    let a: DiffSide?
    let b: DiffSide?
    var id: String { "\(kind)|\(pos)|\(track)|\(name)|\(a?.pos ?? -1)|\(b?.pos ?? -1)" }
}

struct TrackDiff: Decodable, Identifiable {
    let kind: String
    let name: String
    let changes: [String]
    var id: String { kind + "|" + name }
}

struct MarkerDiff: Decodable, Identifiable {
    let kind: String
    let name: String
    let pos: Double
    let region: Bool
    let end: Double
    let changes: [String]
    var id: String { "\(kind)|\(pos)|\(name)" }
}

struct ProjectDiff: Decodable, Identifiable {
    let prop: String
    let a: String
    let b: String
    var id: String { prop }
}

struct Summary: Decodable {
    let items_added: Int, items_removed: Int, items_changed: Int, items_unchanged: Int, items_a: Int, items_b: Int
    let tracks_added: Int, tracks_removed: Int, tracks_changed: Int
    let markers_added: Int, markers_removed: Int, markers_changed: Int
}

struct DiffResult: Decodable {
    let warning: String?
    let a: String
    let b: String
    let summary: Summary
    let project: [ProjectDiff]
    let tracks: [TrackDiff]
    let items: [ItemDiff]
    let markers: [MarkerDiff]
}

func fmtTime(_ sec: Double) -> String {
    let ms = Int((abs(sec) * 1000).rounded())
    let h = ms / 3_600_000, m = (ms / 60_000) % 60, s = (ms / 1000) % 60, f = ms % 1000
    return String(format: "%@%d:%02d:%02d.%03d", sec < 0 ? "-" : "", h, m, s, f)
}

func kindColor(_ kind: String) -> Color {
    switch kind {
    case "added": return Color(red: 0.25, green: 0.73, blue: 0.31)
    case "removed": return Color(red: 0.90, green: 0.28, blue: 0.30)
    default: return Color(red: 0.89, green: 0.63, blue: 0.03)
    }
}

func kindSymbol(_ kind: String) -> String {
    switch kind {
    case "added": return "+"
    case "removed": return "−"
    default: return "~"
    }
}

/// Файл .rpp или папка проекта Reaper → (.rpp, сколько .rpp найдено). В папке: имя как у папки, иначе самый свежий.
func resolveRpp(_ url: URL) -> (url: URL, count: Int)? {
    var isDir: ObjCBool = false
    guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) else { return nil }
    if !isDir.boolValue { return url.pathExtension.lowercased() == "rpp" ? (url, 1) : nil }
    func rpps(in dir: URL) -> [URL] {
        let items = (try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: [.contentModificationDateKey, .isDirectoryKey], options: [.skipsHiddenFiles])) ?? []
        return items.filter { $0.pathExtension.lowercased() == "rpp" }
    }
    var found = rpps(in: url)
    if found.isEmpty {                                   // на один уровень глубже
        let subs = (try? FileManager.default.contentsOfDirectory(at: url, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles])) ?? []
        for s in subs where (try? s.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true { found += rpps(in: s) }
    }
    guard !found.isEmpty else { return nil }
    let want = url.lastPathComponent.lowercased() + ".rpp"
    func date(_ u: URL) -> Date { (try? u.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast }
    func mine(_ u: URL) -> Bool { u.lastPathComponent.range(of: #"_diff\d*\.rpp$"#, options: [.regularExpression, .caseInsensitive]) != nil }
    let best = found.sorted {                            // имя как у папки, затем не «_diff» (наши сессии различий), затем свежий
        let p = $0.lastPathComponent.lowercased() == want, q = $1.lastPathComponent.lowercased() == want
        if p != q { return p }
        if mine($0) != mine($1) { return !mine($0) }
        return date($0) > date($1)
    }[0]
    return (best, found.count)
}

// MARK: - Модель

enum DiffPhase { case idle, comparing, ready, failed }
enum DiffTab: String, CaseIterable, Identifiable {
    case items = "Items", tracks = "Tracks", markers = "Markers", project = "Project"
    var id: String { rawValue }
}
enum KindFilter: String, CaseIterable, Identifiable {
    case all = "All", added = "Added", changed = "Changed", removed = "Removed"
    var id: String { rawValue }
    var kind: String? { self == .all ? nil : rawValue.lowercased() }
}

@MainActor
final class DiffModel: ObservableObject {
    @Published var aURL: URL?
    @Published var bURL: URL?
    @Published var tolMs: Double = 0.01          // допуск по времени, мс
    @Published var phase: DiffPhase = .idle
    @Published var result: DiffResult?
    @Published var message = ""
    @Published var inputNote = ""                // «из папки…» / ошибка выбора
    @Published var progress: Double = 0

    // что класть в сессию различий
    @Published var incAdded = true
    @Published var incChanged = true
    @Published var incRemoved = true
    @Published var incOld = true
    @Published var itemMarkers = true
    @Published var keepTracks = false
    // что учитывать при сравнении дорожек
    @Published var cmpOrder = true
    @Published var cmpFX = true
    @Published var cmpEnv = true
    @Published var cmpState = true
    @Published var savedURL: URL?
    @Published var saveMessage = ""
    @Published var saving = false
    // конвертация в сессию Pro Tools (без промежуточного .rpp)
    @Published var ptCopyMedia = true
    @Published var ptName = ""
    @Published var ptRunning = false
    @Published var ptProgress: Double = 0
    @Published var ptStatus = ""
    @Published var ptResult: URL?
    @Published var ptFailed = false

    nonisolated let python = "/usr/bin/python3"
    nonisolated var script: String {
        if let r = Bundle.main.resourcePath {
            let p = r + "/ptx_tools/rpp_diff.py"
            if FileManager.default.fileExists(atPath: p) { return p }
        }
        return FileManager.default.currentDirectoryPath + "/ptx_tools/rpp_diff.py"
    }

    /// Принимает .rpp или папку проекта Reaper.
    var compareFlags: [String] {
        var f: [String] = []
        if !cmpOrder { f.append("--no-order") }
        if !cmpFX { f.append("--no-fx") }
        if !cmpEnv { f.append("--no-env") }
        if !cmpState { f.append("--no-state") }
        return f
    }

    func set(_ input: URL, isA: Bool) {
        guard let (url, count) = resolveRpp(input) else {
            inputNote = "“\(input.lastPathComponent)” is not a Reaper project: no .rpp found."
            return
        }
        inputNote = count > 1 ? "Folder “\(input.lastPathComponent)” has \(count) .rpp files — using \(url.lastPathComponent)." : ""
        if isA { aURL = url } else { bURL = url }
        savedURL = nil
        if aURL != nil && bURL != nil { compare() }
    }

    func swap() {
        let t = aURL; aURL = bURL; bURL = t
        if aURL != nil && bURL != nil { compare() }
    }

    /// Запускает движок; stdout+stderr собирает целиком (прогресс — короткие строки).
    nonisolated func runEngine(_ args: [String], onLine: (@Sendable (String) -> Void)? = nil) async -> (Int32, String) {
        let py = python, sc = script
        return await Task.detached { () -> (Int32, String) in
            let p = Process()
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = [sc] + args
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            do { try p.run() } catch { return (-1, "Cannot start Python: \(error.localizedDescription)") }
            var all = ""
            let h = pipe.fileHandleForReading
            while true {
                let d = h.availableData
                if d.isEmpty { break }
                if let s = String(data: d, encoding: .utf8) {
                    all += s
                    if let cb = onLine { s.split(whereSeparator: \.isNewline).forEach { cb(String($0)) } }
                }
            }
            p.waitUntilExit()
            return (p.terminationStatus, all)
        }.value
    }

    func compare() {
        guard let a = aURL, let b = bURL else { return }
        phase = .comparing
        progress = 0
        message = "Comparing…"
        result = nil
        let tmp = NSTemporaryDirectory() + "rdiff_\(UUID().uuidString).json"
        let tol = tolMs / 1000
        Task {
            let (code, out) = await runEngine([a.path, b.path, "--tol", String(tol), "--json", tmp] + compareFlags) { [weak self] line in
                // "Comparing items: 300/648"
                if let r = line.range(of: #"(\d+)/(\d+)$"#, options: .regularExpression) {
                    let n = line[r].split(separator: "/").compactMap { Double($0) }
                    if n.count == 2, n[1] > 0 {
                        Task { @MainActor in self?.progress = n[0] / n[1]; self?.message = String(line.prefix(while: { $0 != ":" })) + "…" }
                    }
                }
            }
            defer { try? FileManager.default.removeItem(atPath: tmp) }
            if code == 0, let data = FileManager.default.contents(atPath: tmp),
               let r = try? JSONDecoder().decode(DiffResult.self, from: data) {
                result = r
                phase = .ready
                message = ""
            } else {
                phase = .failed
                message = out.split(separator: "\n").last.map(String.init) ?? "Comparison failed"
            }
        }
    }

    /// Что класть в сессию различий (общие флаги для .rpp и Pro Tools).
    var includeFlags: [String] {
        var args: [String] = []
        if !incAdded { args.append("--no-added") }
        if !incChanged { args.append("--no-changed") }
        if !incRemoved { args.append("--no-removed") }
        if !incOld { args.append("--no-old") }
        if !itemMarkers { args.append("--no-item-markers") }
        if keepTracks { args.append("--keep-unchanged-tracks") }
        return args
    }

    var defaultPTName: String { (bURL?.deletingPathExtension().lastPathComponent ?? "session") + " diff" }

    func save(to url: URL) {
        guard let a = aURL, let b = bURL else { return }
        let args = [a.path, b.path, "--tol", String(tolMs / 1000), "--out", url.path] + compareFlags + includeFlags
        saving = true
        saveMessage = "Saving…"
        Task {
            let (code, out) = await runEngine(args)
            saving = false
            if code == 0, out.contains("Done:") {
                savedURL = url
                saveMessage = out.split(separator: "\n").first(where: { $0.hasPrefix("Items from") }).map(String.init) ?? "Saved"
            } else {
                savedURL = nil
                saveMessage = "Failed: " + (out.split(separator: "\n").last.map(String.init) ?? "unknown error")
            }
        }
    }

    /// Различия → сессия Pro Tools напрямую движком «Reaper to Pro Tools»; промежуточный .rpp не создаётся.
    func createProTools(in folder: URL) {
        guard let a = aURL, let b = bURL else { return }
        let name = ptName.trimmingCharacters(in: .whitespaces).isEmpty ? defaultPTName : ptName.trimmingCharacters(in: .whitespaces)
        var args = [a.path, b.path, "--tol", String(tolMs / 1000), "--pt", folder.path, "--pt-name", name] + compareFlags + includeFlags
        if !ptCopyMedia { args.append("--pt-link") }
        ptRunning = true
        ptFailed = false
        ptResult = nil
        ptProgress = 0
        ptStatus = "Starting…"
        Task {
            let (code, out) = await runEngine(args) { [weak self] line in
                // "Comparing items: 300/648", "Placing clips: 120/501"
                if let r = line.range(of: #"^(.+): (\d+)/(\d+)$"#, options: .regularExpression) {
                    let parts = String(line[r]).components(separatedBy: ": ")
                    let n = (parts.last ?? "").split(separator: "/").compactMap { Double($0) }
                    if parts.count >= 2, n.count == 2, n[1] > 0 {
                        Task { @MainActor in self?.ptProgress = n[0] / n[1]; self?.ptStatus = "\(parts[0]) \(Int(n[0]))/\(Int(n[1]))" }
                    }
                }
            }
            ptRunning = false
            let lines = out.split(separator: "\n").map(String.init)
            if code == 0, let done = lines.last(where: { $0.hasPrefix("Done:") }) {
                ptProgress = 1
                ptStatus = done
                // "Done: 8 clips on 2 tracks -> /path/name.ptx" (или папка с несколькими сессиями)
                if let r = done.range(of: " -> ") {
                    var u = URL(fileURLWithPath: String(done[r.upperBound...]))
                    if !FileManager.default.fileExists(atPath: u.path) { u = folder }
                    ptResult = u
                }
            } else {
                ptFailed = true
                ptStatus = "Failed: " + (lines.last(where: { !$0.contains("/") || $0.hasPrefix("Error") || $0.hasPrefix("Warning") }) ?? lines.last ?? "unknown error")
            }
        }
    }

    func reportText() async -> String? {
        guard let a = aURL, let b = bURL else { return nil }
        let (code, out) = await runEngine([a.path, b.path, "--tol", String(tolMs / 1000), "--report"] + compareFlags)
        guard code == 0 else { return nil }
        // убираем строки прогресса
        return out.split(separator: "\n", omittingEmptySubsequences: false)
            .filter { $0.range(of: #"^[A-Za-z ]+: \d+/\d+$"#, options: .regularExpression) == nil }
            .joined(separator: "\n")
    }
}

// MARK: - Вьюхи

struct Card<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 12, weight: .semibold)).padding(.leading, 4)
            VStack(alignment: .leading, spacing: 8) { content }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.primary.opacity(0.08)))
        }
    }
}

struct Detail: View {
    let text: String
    var body: some View {
        Text(text).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
    }
}

struct FileCard: View {
    let title: String
    let subtitle: String
    let url: URL?
    let choose: () -> Void
    @State private var targeted = false
    let onDropURL: (URL) -> Void

    var body: some View {
        Card(title: title) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(url?.lastPathComponent ?? "Drop a .rpp or a project folder")
                        .font(.system(size: 12, weight: .medium)).lineLimit(1).truncationMode(.middle)
                    Detail(text: url.map { $0.deletingLastPathComponent().path } ?? subtitle)
                        .lineLimit(1).truncationMode(.head)
                }
                Spacer()
                Button("Browse…", action: choose)
            }
        }
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.accentColor, lineWidth: targeted ? 2 : 0).padding(.top, 20))
        .onDrop(of: [.fileURL], isTargeted: $targeted) { providers in
            providers.first?.loadItem(forTypeIdentifier: UTType.fileURL.identifier) { item, _ in
                if let d = item as? Data, let u = URL(dataRepresentation: d, relativeTo: nil) {
                    Task { @MainActor in onDropURL(u) }
                }
            }
            return true
        }
    }
}

struct Chip: View {
    let kind: String
    let count: Int
    let label: String
    var body: some View {
        HStack(spacing: 5) {
            Text(kindSymbol(kind)).font(.system(size: 12, weight: .bold)).foregroundStyle(kindColor(kind))
            Text("\(count) \(label)").font(.system(size: 12))
        }
        .padding(.horizontal, 9).padding(.vertical, 4)
        .background(Capsule().fill(kindColor(kind).opacity(0.14)))
    }
}

struct ContentView: View {
    @ObservedObject var m: DiffModel
    @State private var tab: DiffTab = .items
    @State private var filter: KindFilter = .all
    @State private var search = ""

    var body: some View {
        VStack(spacing: 12) {
            HStack(alignment: .bottom, spacing: 8) {
                FileCard(title: "A — old session", subtitle: "Choose or drop the older .rpp or folder", url: m.aURL,
                         choose: { choose(isA: true) }, onDropURL: { open($0, isA: true) })
                Button { m.swap() } label: { Image(systemName: "arrow.left.arrow.right") }
                    .help("Swap A and B").padding(.bottom, 14).disabled(m.aURL == nil && m.bURL == nil)
                FileCard(title: "B — new session", subtitle: "Choose or drop the newer .rpp or folder", url: m.bURL,
                         choose: { choose(isA: false) }, onDropURL: { open($0, isA: false) })
            }
            optionsRow
            statusRow
            if let w = m.result?.warning, !w.isEmpty {
                Label(w, systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 12)).foregroundStyle(.orange)
                    .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.12)))
            }
            if !m.inputNote.isEmpty {
                Text(m.inputNote).font(.system(size: 11)).foregroundStyle(.orange).frame(maxWidth: .infinity, alignment: .leading)
            }
            if let r = m.result {
                resultArea(r)
                saveCard
            } else {
                Spacer(minLength: 0)
            }
        }
        .padding(16)
        .frame(minWidth: 960, minHeight: 640)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    func open(_ u: URL, isA: Bool) { m.set(u, isA: isA) }

    func choose(isA: Bool) {
        let p = NSOpenPanel()
        p.allowedContentTypes = [UTType(filenameExtension: "rpp") ?? .data, .folder]
        p.canChooseDirectories = true
        p.message = "Choose a Reaper project (.rpp) or its folder"
        if p.runModal() == .OK, let u = p.url { m.set(u, isA: isA) }
    }

    // MARK: что сравнивать

    var optionsRow: some View {
        HStack(spacing: 16) {
            Text("Track comparison").font(.system(size: 11)).foregroundStyle(.secondary)
            Toggle("Track order", isOn: $m.cmpOrder).help("Report tracks that were moved to another position")
            Toggle("Plugins", isOn: $m.cmpFX).help("FX chains: added/removed/reordered, bypass, wet, preset, settings changed")
            Toggle("Envelopes", isOn: $m.cmpEnv).help("Track automation: volume, pan, mute, width, sends, plugin parameters")
            Toggle("Mute / solo / rec arm", isOn: $m.cmpState).help("Track mute, solo and record arm")
            Spacer()
        }
        .toggleStyle(.checkbox).font(.system(size: 12))
        .onChange(of: m.cmpOrder) { _, _ in recompare() }
        .onChange(of: m.cmpFX) { _, _ in recompare() }
        .onChange(of: m.cmpEnv) { _, _ in recompare() }
        .onChange(of: m.cmpState) { _, _ in recompare() }
    }

    func recompare() { if m.aURL != nil && m.bURL != nil && m.phase != .comparing { m.compare() } }

    // MARK: статус

    var statusRow: some View {
        HStack(spacing: 10) {
            switch m.phase {
            case .idle:
                Detail(text: "Choose two Reaper sessions: A (older) and B (newer). Differences are found by item GUID, then by media file and position.")
            case .comparing:
                ProgressView(value: m.progress).frame(width: 160)
                Detail(text: m.message)
            case .failed:
                Label(m.message, systemImage: "exclamationmark.triangle.fill").font(.system(size: 12)).foregroundStyle(.red)
            case .ready:
                if let s = m.result?.summary {
                    Chip(kind: "added", count: s.items_added, label: "added")
                    Chip(kind: "changed", count: s.items_changed, label: "changed")
                    Chip(kind: "removed", count: s.items_removed, label: "removed")
                    Detail(text: "\(s.items_unchanged) unchanged · A: \(s.items_a) items, B: \(s.items_b) items")
                }
            }
            Spacer()
            HStack(spacing: 4) {
                Text("Tolerance").font(.system(size: 11)).foregroundStyle(.secondary)
                TextField("", value: $m.tolMs, format: .number).frame(width: 52).textFieldStyle(.roundedBorder)
                    .onSubmit { m.compare() }
                Text("ms").font(.system(size: 11)).foregroundStyle(.secondary)
                Button("Compare") { m.compare() }.disabled(m.aURL == nil || m.bURL == nil || m.phase == .comparing)
            }
        }
        .frame(minHeight: 26)
    }

    // MARK: результат

    func resultArea(_ r: DiffResult) -> some View {
        VStack(spacing: 8) {
            HStack {
                Picker("", selection: $tab) {
                    ForEach(DiffTab.allCases) { t in Text(tabTitle(t, r)).tag(t) }
                }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 380)
                if tab == .items || tab == .markers {
                    Picker("", selection: $filter) {
                        ForEach(KindFilter.allCases) { f in Text(f.rawValue).tag(f) }
                    }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 300)
                }
                Spacer()
                if tab == .items {
                    TextField("Search track or name", text: $search).textFieldStyle(.roundedBorder).frame(width: 190)
                }
                Menu("Report") {
                    Button("Copy as text") { Task { if let t = await m.reportText() {
                        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(t, forType: .string) } } }
                    Button("Save as text file…") { saveReport() }
                }.fixedSize()
            }
            Group {
                switch tab {
                case .items: itemsList(r)
                case .tracks: tracksList(r)
                case .markers: markersList(r)
                case .project: projectList(r)
                }
            }
            .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.primary.opacity(0.08)))
        }
    }

    func tabTitle(_ t: DiffTab, _ r: DiffResult) -> String {
        switch t {
        case .items: return "Items \(r.summary.items_added + r.summary.items_changed + r.summary.items_removed)"
        case .tracks: return "Tracks \(r.tracks.count)"
        case .markers: return "Markers \(r.markers.count)"
        case .project: return "Project \(r.project.count)"
        }
    }

    func itemsList(_ r: DiffResult) -> some View {
        let q = search.lowercased()
        let rows = r.items.filter { d in
            (filter.kind == nil || d.kind == filter.kind) &&
            (q.isEmpty || d.track.lowercased().contains(q) || d.name.lowercased().contains(q))
        }
        return Group {
            if rows.isEmpty { emptyNote(r.items.isEmpty ? "No item differences." : "Nothing matches the filter.") }
            else {
                List(rows) { d in
                    HStack(alignment: .top, spacing: 10) {
                        Text(kindSymbol(d.kind)).font(.system(size: 15, weight: .bold)).foregroundStyle(kindColor(d.kind)).frame(width: 16)
                        Text(fmtTime(d.pos)).font(.system(size: 12, design: .monospaced)).frame(width: 90, alignment: .leading)
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 6) {
                                Text(d.track).font(.system(size: 12, weight: .semibold))
                                Text(d.name).font(.system(size: 12)).lineLimit(1).truncationMode(.middle)
                            }
                            ForEach(d.changes, id: \.self) { c in
                                Text(c).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            }
                            if d.kind != "changed", let s = d.b ?? d.a {
                                Text("length \(String(format: "%.3f", s.length)) s").font(.system(size: 11)).foregroundStyle(.secondary)
                            }
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
        }
    }

    func tracksList(_ r: DiffResult) -> some View {
        Group {
            if r.tracks.isEmpty { emptyNote("Tracks are identical.") }
            else {
                List(r.tracks) { t in
                    HStack(alignment: .top, spacing: 10) {
                        Text(kindSymbol(t.kind)).font(.system(size: 15, weight: .bold)).foregroundStyle(kindColor(t.kind)).frame(width: 16)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(t.name).font(.system(size: 12, weight: .semibold))
                            ForEach(t.changes, id: \.self) { c in Text(c).font(.system(size: 11)).foregroundStyle(.secondary) }
                        }
                    }.padding(.vertical, 2)
                }
            }
        }
    }

    func markersList(_ r: DiffResult) -> some View {
        let rows = r.markers.filter { filter.kind == nil || $0.kind == filter.kind }
        return Group {
            if rows.isEmpty { emptyNote(r.markers.isEmpty ? "Markers are identical." : "Nothing matches the filter.") }
            else {
                List(rows) { mk in
                    HStack(alignment: .top, spacing: 10) {
                        Text(kindSymbol(mk.kind)).font(.system(size: 15, weight: .bold)).foregroundStyle(kindColor(mk.kind)).frame(width: 16)
                        Text(fmtTime(mk.pos)).font(.system(size: 12, design: .monospaced)).frame(width: 90, alignment: .leading)
                        VStack(alignment: .leading, spacing: 2) {
                            Text((mk.region ? "Region " : "Marker ") + mk.name).font(.system(size: 12, weight: .semibold))
                            ForEach(mk.changes, id: \.self) { c in Text(c).font(.system(size: 11)).foregroundStyle(.secondary) }
                        }
                    }.padding(.vertical, 2)
                }
            }
        }
    }

    func projectList(_ r: DiffResult) -> some View {
        Group {
            if r.project.isEmpty { emptyNote("Project settings (sample rate, frame rate, tempo, time signature) are identical.") }
            else {
                List(r.project) { p in
                    HStack {
                        Text(p.prop.capitalized).font(.system(size: 12, weight: .semibold)).frame(width: 140, alignment: .leading)
                        Text("\(p.a)  →  \(p.b)").font(.system(size: 12))
                    }
                }
            }
        }
    }

    func emptyNote(_ s: String) -> some View {
        Text(s).font(.system(size: 12)).foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: сохранение

    var saveCard: some View {
        Card(title: "Save differences") {
            HStack(spacing: 16) {
                Toggle("Added (green)", isOn: $m.incAdded)
                Toggle("Changed — new (yellow)", isOn: $m.incChanged)
                Toggle("Removed from A (red)", isOn: $m.incRemoved)
                Toggle("Old versions of changed (grey)", isOn: $m.incOld)
                Toggle("Markers for each difference", isOn: $m.itemMarkers)
                Toggle("Keep unchanged tracks", isOn: $m.keepTracks)
            }.toggleStyle(.checkbox).font(.system(size: 12))
            HStack {
                Detail(text: "Only tracks with differences are kept (folders and sends are dropped); only the differing items stay. Items from A go to tracks “A > name”. Descriptions are in item notes.")
                Spacer()
                if m.saving { ProgressView().controlSize(.small) }
                if let u = m.savedURL {
                    Button("Reveal") { NSWorkspace.shared.activateFileViewerSelecting([u]) }
                    Button("Open in Reaper") { NSWorkspace.shared.open(u) }
                }
                Button("Save Reaper Session…") { saveSession() }.disabled(m.saving || m.ptRunning).keyboardShortcut("s")
            }
            if !m.saveMessage.isEmpty {
                Text(m.saveMessage).font(.system(size: 11)).foregroundStyle(m.savedURL == nil && !m.saving ? .red : .secondary)
            }
            Divider()
            HStack(spacing: 12) {
                Text("Pro Tools session").font(.system(size: 12, weight: .semibold))
                Toggle("Copy media", isOn: $m.ptCopyMedia).toggleStyle(.checkbox).font(.system(size: 12))
                    .help("Off: link to the original audio files instead of copying them")
                TextField(m.defaultPTName, text: $m.ptName).textFieldStyle(.roundedBorder).frame(maxWidth: 240)
                Spacer()
                if m.ptRunning { ProgressView(value: m.ptProgress).frame(width: 120) }
                if let u = m.ptResult {
                    Button("Reveal") { NSWorkspace.shared.activateFileViewerSelecting([u]) }
                    if u.pathExtension.lowercased() == "ptx" { Button("Open in Pro Tools") { NSWorkspace.shared.open(u) } }
                }
                Button("Create Pro Tools Session…") { createPT() }.disabled(m.saving || m.ptRunning || !converterInstalled)
                    .help(converterInstalled ? "" : "Needs the Reaper to Pro Tools app installed in Applications")
            }
            HStack {
                Detail(text: "Converted straight from the differences with the Reaper to Pro Tools engine; no .rpp file is created. Item colors and notes are not transferred; markers are.")
                Spacer()
            }
            if !m.ptStatus.isEmpty {
                Text(m.ptStatus).font(.system(size: 11)).foregroundStyle(m.ptFailed ? .red : .secondary).lineLimit(2)
            }
        }
    }

    func createPT() {
        let p = NSOpenPanel()
        p.canChooseFiles = false
        p.canChooseDirectories = true
        p.canCreateDirectories = true
        p.prompt = "Create Here"
        p.message = "Choose the folder for the Pro Tools session"
        p.directoryURL = m.bURL?.deletingLastPathComponent().deletingLastPathComponent()
        if p.runModal() == .OK, let u = p.url { m.createProTools(in: u) }
    }

    func saveSession() {
        guard let b = m.bURL else { return }
        let p = NSSavePanel()
        p.allowedContentTypes = [UTType(filenameExtension: "rpp") ?? .data]
        p.nameFieldStringValue = b.deletingPathExtension().lastPathComponent + "_diff.rpp"
        // не в папку проекта B: сессии различий не должны путаться с самим проектом
        let projDir = b.deletingLastPathComponent()
        p.directoryURL = projDir.deletingLastPathComponent()
        if p.runModal() == .OK, let u = p.url { m.save(to: u) }
    }

    func saveReport() {
        let p = NSSavePanel()
        p.allowedContentTypes = [.plainText]
        p.nameFieldStringValue = "rpp_diff_report.txt"
        guard p.runModal() == .OK, let u = p.url else { return }
        Task { if let t = await m.reportText() { try? t.write(to: u, atomically: true, encoding: .utf8) } }
    }
}

// Скрытая отладка: RD_SNAPSHOT=/путь/out.png — сохранить PNG своего окна и выйти.
@MainActor
func scheduleSnapshot() {
    guard let path = ProcessInfo.processInfo.environment["RD_SNAPSHOT"] else { return }
    DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
        if let view = NSApp.windows.first(where: { $0.isVisible })?.contentView,
           let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
            view.cacheDisplay(in: view.bounds, to: rep)
            if let png = rep.representation(using: .png, properties: [:]) { try? png.write(to: URL(fileURLWithPath: path)) }
        }
        NSApp.terminate(nil)
    }
}

/// Сессию Pro Tools строит движок программы «Reaper to Pro Tools» (rpp_diff.py ищет его там же).
let converterInstalled = [NSHomeDirectory() + "/ReaperToProTools/ptx_tools",
                          "/Applications/Reaper to Pro Tools.app/Contents/Resources/ptx_tools",
                          NSHomeDirectory() + "/Applications/Reaper to Pro Tools.app/Contents/Resources/ptx_tools"]
    .contains { FileManager.default.fileExists(atPath: $0 + "/reaper_to_ptx.py") }

@main
struct ReaperDiffApp: App {
    @StateObject private var m = DiffModel()
    @StateObject private var updater = Updater()
    var body: some Scene {
        WindowGroup {
            ContentView(m: m)
                .modifier(UpdateUI(u: updater))
                .onAppear {
                    updater.canInstall = { [weak m] in !((m?.saving ?? false) || (m?.ptRunning ?? false)) }
                    scheduleSnapshot()
                    // Отладка: RD_A / RD_B = пути к .rpp (файл в argv окно подавляет, см. заметки).
                    let env = ProcessInfo.processInfo.environment
                    if let a = env["RD_A"] { m.aURL = URL(fileURLWithPath: a) }
                    if let b = env["RD_B"] { m.bURL = URL(fileURLWithPath: b) }
                    if m.aURL != nil && m.bURL != nil { m.compare() }
                }
        }
        .defaultSize(width: 900, height: 700)
        .commands {
            CommandGroup(after: .appInfo) { Button("Check for Updates…") { updater.check(manual: true) } }
        }
    }
}
