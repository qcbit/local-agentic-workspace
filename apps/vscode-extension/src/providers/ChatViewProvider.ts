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
            localResourceRoots: [
                vscode.Uri.joinPath(this._extensionUri, 'out')
            ]
        };

        const scriptUri = webviewView.webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'out', 'chat.js')
        );

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

        // Listen for the agent's inner monologue from IPC
        this._udsClient.on('agentThinking', (message: string) => {
            if (this._view) {
                this._view.webview.postMessage({ 
                    command: 'agentThinking', 
                    text: message 
                });
            }
        });

        // Listen for user actions from the React webview
        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.command || data.type) {
                case 'executeTask':
                    await this._executeTask(data.goal, data.autoApprove ?? false);
                    break;
                case 'stop_agent':
                    console.log("🛑 Received stop signal from UI. Routing to IPC...");
                    if (this._udsClient) {
                        this._udsClient.cancelTask();
                        webviewView.webview.postMessage({ type: 'agent_stopped' });
                    }
                    break;
                case 'reset_session':
                    this._udsClient.request('reset_session', {});
                    break;
                case 'restore_session':
                    this._udsClient.request('restore_session', { history: data.messages });
                    break;
            }
        });
    }

    /**
     * Executes an agent task via the IPC socket and returns the result to the UI.
     */
    private async _executeTask(goal: string, autoApprove: boolean = false) {
        if (!this._view) return;

        try {
            const payload = {
                goal: goal,
                auto_approve: autoApprove
            };
            const result = await this._udsClient.request('execute_agent_task', payload);

            let responseText = "Task completed.";
            if (result && result.final_observation) {
                responseText = result.final_observation;
            } else if (result) {
                responseText = JSON.stringify(result, null, 2);
            }

            this._view.webview.postMessage({ command: 'agentResponse', text: responseText });
        } catch (error: any) {
            this._view.webview.postMessage({ 
                command: 'agentError', 
                text: error.message || 'Error communicating with orchestrator.' 
            });
        }
    }

    /**
     * Proactively injects a prompt into the chat stream from external events (e.g. terminal errors).
     */
    public injectProactiveMessage(displayText: string, executionText: string) {
        if (this._view) {
            // Display the clean prompt in the React message list
            this._view.webview.postMessage({ command: 'injectMessage', text: displayText });
            
            // Dispatch the hidden, rule-heavy task directly to the orchestrator
            this._executeTask(executionText, false);
        } else {
            vscode.commands.executeCommand('localAgenticWorkspace.chatView.focus').then(() => {
                setTimeout(() => this.injectProactiveMessage(displayText, executionText), 500);
            });
        }
    }
}
