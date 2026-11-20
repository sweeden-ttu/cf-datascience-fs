import CFDSKit
import SwiftUI

/// First screen: create a free account or sign in at datascientist.cloudfronts.group.
struct SignInView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            Image(systemName: "externaldrive.connected.to.line.below")
                .font(.system(size: 64))
                .foregroundStyle(.tint)
            VStack(spacing: 8) {
                Text("Cloudfronts for Data Scientists")
                    .font(.title2.bold())
                    .multilineTextAlignment(.center)
                Text(
                    "Your own \(freeTier) of storage, free, plus the shared Kaggle datasets. Open them right in the Files app."
                )
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            }
            .padding(.horizontal)

            VStack(spacing: 12) {
                Button {
                    Task { await model.signIn(createAccount: true) }
                } label: {
                    Text("Create free account").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)

                Button {
                    Task { await model.signIn(createAccount: false) }
                } label: {
                    Text("Sign in").frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
            }
            .controlSize(.large)
            .disabled(model.isWorking)
            .padding(.horizontal, 32)

            if model.isWorking { ProgressView() }
            Spacer()
            Link("About the free tier", destination: model.config.signupWebURL)
                .font(.footnote)
                .padding(.bottom)
        }
    }

    private var freeTier: String {
        ByteCountFormatter.string(
            fromByteCount: CFDSConfiguration.freeTierBytes, countStyle: .binary)
    }
}
