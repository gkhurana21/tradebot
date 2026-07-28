// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "TradebotDashboard",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "TradebotDashboard", targets: ["TradebotDashboard"]),
        .library(name: "TradebotKit", targets: ["TradebotKit"]),
    ],
    targets: [
        // The UI layer is deliberately thin. Everything worth testing --
        // decoding, polling, retry, derived state -- lives in TradebotKit so
        // it can be verified without launching a window.
        .executableTarget(
            name: "TradebotDashboard",
            dependencies: ["TradebotKit"]
        ),
        .target(name: "TradebotKit"),
        .testTarget(name: "TradebotKitTests", dependencies: ["TradebotKit"]),
    ]
)
