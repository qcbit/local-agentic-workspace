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

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [vscode.Uri.joinPath(this._extensionUri, 'out')]
        };

        const scriptUri = webviewView.webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'out', 'chat.js')
        );

        webviewView.webview.html = this._getHtmlForWebview(scriptUri);

        // Listen for the prompt from React, send it to Python via IPC
        webviewView.webview.onDidReceiveMessage(async (data) => {
            if (data.command === 'executeTask') {
                try {
                    const result = await this._udsClient.request('execute_agent_task', { goal: data.goal });
                    
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
            <div id="root"></div>
            <script src="${scriptUri}"></script>
        </body>
        </html>
        `;
    }
}
