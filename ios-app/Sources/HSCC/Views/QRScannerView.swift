import SwiftUI
import AVFoundation

/// Camera QR-code scanner presented modally from Settings ("Scan QR").
///
/// The scanning itself is AVFoundation (`AVCaptureSession` +
/// `AVCaptureMetadataOutput` observing `.qr`). Because driving a live camera
/// in SwiftUI is awkward, the heavy lifting lives in a `UIViewRepresentable`
/// (`QRCameraPreview`) and this view owns the permission + presentation state:
///   * request camera access on appear (the `NSCameraUsageDescription` build
///     setting makes the system show a real reason for the prompt);
///   * if granted → show the live preview and start scanning;
///   * if denied → show an honest explanation + a button that jumps to the
///     app's row in the system Settings, never a dead black screen;
///   * if undetermined/limited → keep prompting.
///
/// The presenter passes `onScan(String)`; this view hands over the raw payload
/// text and lets the caller decode + validate it, so the scanner never decides
/// what a code means. After a code is read the session pauses so a stale frame
/// doesn't re-fire while the operator decides.
struct QRScannerView: View {
    /// Delivers the raw scanned payload text (unvalidated).
    ///
    /// `@MainActor` so the callback always runs on the main actor. The
    /// AVFoundation metadata delegate fires on a background dispatch queue; a
    /// non-isolated closure calling a `@MainActor` sync handler would mutate
    /// SwiftUI `@State` on that background thread (a silent data race — the
    /// compiler provides no hop under Swift 5.9's minimal concurrency). MainActor
    /// isolation on the closure type inserts the hop at every call site.
    let onScan: @MainActor (String) -> Void
    /// Dismisses the scanner (the sheet's Cancel / the trailing close button).
    var onCancel: () -> Void = {}

    @State private var authorization: AVAuthorizationStatus = .notDetermined

    var body: some View {
        NavigationStack {
            Group {
                switch authorization {
                case .authorized:
                    QRCameraPreview(onScan: onScan)
                        .ignoresSafeArea(edges: .bottom)
                case .denied, .restricted:
                    permissionDeniedView
                case .notDetermined:
                    // Brief placeholder while we ask; the request is fired in
                    // onAppear and updates `authorization` on completion.
                    HSLoading("Requesting camera access…")
                @unknown default:
                    permissionDeniedView
                }
            }
            .navigationTitle("Scan setup QR")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { onCancel() }
                }
            }
        }
        .onAppear {
            let status = AVCaptureDevice.authorizationStatus(for: .video)
            authorization = status
            if status == .notDetermined {
                AVCaptureDevice.requestAccess(for: .video) { granted in
                    Task { @MainActor in
                        authorization = granted ? .authorized : .denied
                    }
                }
            }
        }
    }

    /// A real explanation plus a one-tap path to the system Settings, instead
    /// of a dead screen — an operator who denied access on a first run can
    /// understand why the camera won't open and fix it in one step.
    private var permissionDeniedView: some View {
        VStack(spacing: 16) {
            Image(systemName: "camera.fill")
                .font(.system(size: 44))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text("Camera access is off")
                .font(.headline)
            Text("HSCC needs the camera only to scan the setup QR code from `hscc api status`. Turn on camera access in Settings to continue.")
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .multilineTextAlignment(.center)
                .padding(.horizontal)
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    UIApplication.shared.open(url)
                }
            }
            .buttonStyle(.borderedProminent)
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// A `UIViewRepresentable` that runs an `AVCaptureSession` and reports QR codes.
///
/// Runs its session on a background queue (AVFoundation requires the session's
/// start/stop off the main thread), captures `.qr` metadata, and pauses after a
/// single read so the same code doesn't fire repeatedly.
private struct QRCameraPreview: UIViewRepresentable {
    let onScan: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onScan: onScan)
    }

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        context.coordinator.start(on: view)
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {
        // Nothing to update — the capture session and preview are static for
        // the life of the scanner.
    }

    static func dismantleUIView(_ uiView: PreviewView, coordinator: Coordinator) {
        coordinator.stop()
    }

    /// The AVFoundation session owner: configures input/output, starts the
    /// session on a background queue, and translates `.qr` reads to `onScan`.
    final class Coordinator: NSObject, AVCaptureMetadataOutputObjectsDelegate {
        private let onScan: (String) -> Void
        private let session = AVCaptureSession()
        private let queue = DispatchQueue(label: "com.hscc.ios.qr-scan")
        private var isRunning = false

        init(onScan: @escaping (String) -> Void) {
            self.onScan = onScan
        }

        func start(on preview: PreviewView) {
            guard !isRunning else { return }
            preview.videoPreviewLayer.session = session
            preview.videoPreviewLayer.videoGravity = .resizeAspectFill

            session.beginConfiguration()
            // Back (world) camera — the one that looks at a printed code.
            guard let device = AVCaptureDevice.default(
                    .builtInWideAngleCamera, for: .video, position: .back),
                  let input = try? AVCaptureDeviceInput(device: device) else {
                session.commitConfiguration()
                return
            }
            guard session.canAddInput(input) else {
                session.commitConfiguration()
                return
            }
            session.addInput(input)

            let output = AVCaptureMetadataOutput()
            guard session.canAddOutput(output) else {
                session.commitConfiguration()
                return
            }
            session.addOutput(output)
            output.setMetadataObjectsDelegate(self, queue: queue)
            output.metadataObjectTypes = [.qr]
            session.commitConfiguration()

            isRunning = true
            // Start on the background queue: starting/stopping the session on
            // the main thread triggers an AVFoundation warning and jank.
            queue.async { [weak self] in
                self?.session.startRunning()
            }
        }

        func stop() {
            guard isRunning else { return }
            isRunning = false
            queue.async { [weak self] in
                self?.session.stopRunning()
            }
        }

        // MARK: - AVCaptureMetadataOutputObjectsDelegate

        func metadataOutput(_ output: AVCaptureMetadataOutput,
                            didOutput metadataObjects: [AVMetadataObject],
                            from connection: AVCaptureConnection) {
            for case let readable as AVMetadataMachineReadableCodeObject in metadataObjects {
                guard readable.type == .qr, let value = readable.stringValue else { continue }
                // Halt the session so a lingering frame can't re-fire while the
                // operator reads/confirms. Stop synchronously on our queue —
                // we're already on it.
                session.stopRunning()
                // Deliver on the main actor: `onScan` is `@MainActor` (it feeds
                // SwiftUI `@State`), and we are on the background AVFoundation
                // queue here.
                Task { @MainActor in
                    onScan(value)
                }
                break
            }
        }
    }

    /// `UIView` whose layer is the video preview layer.
    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var videoPreviewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }
}
