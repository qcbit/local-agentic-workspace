import * as vscode from 'vscode';
import { UdsClient } from '../ipc/UdsClient';

export class ChatViewProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'localAgenticWorkspace.chatView';
    private _view?: vscode.WebviewView;

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _udsClient: UdsClient
    ) { }

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ) {
        this._view = webviewView;

        // 1. Only authorize the 'out' directory
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(this._extensionUri, 'out')
            ]
        };

        // 2. Point directly to the newly isolated chat.js bundle
        const scriptUri = webviewView.webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'out', 'chat.js')
        );

        // 3. Inject a completely clean HTML template without the missing icon
        webviewView.webview.html = `
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Agent Chat</title>
            </head>
            <body>
                <div id="root"></div>
                <script src="${scriptUri}"></script>
            </body>
            </html>
        `;

        // Listen for the prompt from React, send it to Python via IPC
        webviewView.webview.onDidReceiveMessage(async (data) => {
            if (data.command === 'executeTask') {
                try {
                    // Map the React state (autoApprove) to the Python expected key (auto_approve)
                    const payload = {
                        goal: data.goal,
                        auto_approve: data.autoApprove 
                    };
                    const result = await this._udsClient.request('execute_agent_task', payload);

                    // Parse agent observation to string
                    let responseText = "Task completed.";
                    if (result && result.final_observation) {
                        responseText = result.final_observation;
                    } else if (result) {
                        responseText = JSON.stringify(result, null, 2);
                    }

                    // Send the result back to React
                    webviewView.webview.postMessage({ command: 'agentResponse', text: responseText });
                } catch (error: any) {
                    webviewView.webview.postMessage({ command: 'agentError', text: error.message || 'Error communicating with orchestrator.' });
                }
            }
        });
    }

    private _getHtmlForWebview(scriptUri: vscode.Uri) {
        return `
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Agent Chat</title>
        </head>
        <body>
            <!-- This will catch and print any React/JS crashes directly to the sidebar! -->
            <div id="error-output" style="color: #ff6b6b; font-family: monospace; padding: 10px; font-weight: bold; white-space: pre-wrap;"></div>
            
            <div id="root"></div>
            
            <script>
                // Catch standard errors
                window.onerror = function(message, source, lineno, colno, error) {
                    document.getElementById('error-output').textContent += '🚨 ERROR: ' + message + '\\n';
                    return false;
                };
                // Catch promise rejections
                window.addEventListener('unhandledrejection', function(event) {
                    document.getElementById('error-output').textContent += '🚨 PROMISE REJECTION: ' + (event.reason.stack || event.reason) + '\\n';
                });
            </script>
            
            <script src="${scriptUri}"></script>
        </body>
        </html>
    `;
    }
}
