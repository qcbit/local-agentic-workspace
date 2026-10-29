import * as vscode from 'vscode';
import * as path from 'path';
import * as os from 'os';
import * as fs from 'fs';
import * as http from 'http';
import * as modelSpecs from '../modelSpecs.json';
import { UdsClient } from '../ipc/UdsClient';

export class SettingsPanelViewProvider {
    public static currentPanel: SettingsPanelViewProvider | undefined;
    private readonly _panel: vscode.WebviewPanel;
    private _disposables: vscode.Disposable[] = [];

    private constructor(
        panel: vscode.WebviewPanel, 
        private readonly context: vscode.ExtensionContext,
        private readonly udsClient: UdsClient
    ) {
        this._panel = panel;

        const scriptPathOnDisk = vscode.Uri.file(path.join(this.context.extensionPath, 'out', 'webview.js'));
        const scriptUri = this._panel.webview.asWebviewUri(scriptPathOnDisk);

        this._panel.webview.html = this.getHtmlForWebview(scriptUri);

        this._panel.onDidDispose(() => this.dispose(), null, this._disposables);

        this._panel.webview.onDidReceiveMessage(
            async (message) => this.handleMessage(message),
            null,
            this._disposables
        );
    }

    public static createOrShow(context: vscode.ExtensionContext, udsClient: UdsClient) {
        const column = vscode.window.activeTextEditor ? vscode.window.activeTextEditor.viewColumn : undefined;

        if (SettingsPanelViewProvider.currentPanel) {
            SettingsPanelViewProvider.currentPanel._panel.reveal(column);
            return;
        }

        const panel = vscode.window.createWebviewPanel(
            'agentSettings',
            'Agentic Workspace Settings',
            vscode.ViewColumn.One,
            {
                enableScripts: true,
                localResourceRoots: [vscode.Uri.file(path.join(context.extensionPath, 'out'))]
            }
        );

        SettingsPanelViewProvider.currentPanel = new SettingsPanelViewProvider(panel, context, udsClient);
    }

    public postMessage(message: any) {
        this._panel.webview.postMessage(message);
    }

    public dispose() {
        SettingsPanelViewProvider.currentPanel = undefined;
        this._panel.dispose();
        while (this._disposables.length) {
            const x = this._disposables.pop();
            if (x) {
                x.dispose();
            }
        }
    }

    private getHtmlForWebview(scriptUri: vscode.Uri): string {
        return `
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Settings</title>
            </head>
            <body>
                <div id="root"></div>
                <script src="${scriptUri}"></script>
            </body>
            </html>
        `;
    }

    private fetchModels(): Promise<string[]> {
        return new Promise((resolve) => {
            const req = http.get('http://127.0.0.1:11434/api/tags', (res) => {
                let data = '';
                res.on('data', chunk => data += chunk);
                res.on('end', () => {
                    try {
                        const parsed = JSON.parse(data);
                        const models = parsed.models.map((m: any) => m.name);
                        resolve(models);
                    } catch (e) {
                        console.error('Failed to parse models', e);
                        resolve(['llama3:8b', 'qwen2.5-coder:7b']); 
                    }
                });
            }).on('error', (e) => {
                console.error('Failed to reach local LLM API', e);
                resolve(['llama3:8b', 'qwen2.5-coder:7b']);
            });

            req.setTimeout(2000, () => {
                req.destroy();
                resolve(['llama3:8b', 'qwen2.5-coder:7b']);
            });
        });
    }

    private async handleMessage(message: any) {
        if (message.command === 'ready') {
            const availableModels = await this.fetchModels();
            let configData = null;
            try {
                configData = await this.udsClient.request('get_config', {});
            } catch (error: any) {
                console.error('Failed to fetch config from backend:', error);
            }

            if (!configData || Object.keys(configData).length === 0 || !configData.profiles) {
                configData = {
                    active_profile: 'home',
                    profiles: {
                        home: {
                            llm: { model_name: 'llama3:8b', endpoint_url: 'http://127.0.0.1:11434/v1/chat/completions' }
                        }
                    }
                };
            }

            if (configData && configData.profiles) {
                Object.values(configData.profiles).forEach((profile: any) => {
                    const savedModel = profile?.llm?.model_name || profile?.llm?.model;
                    if (savedModel && !availableModels.includes(savedModel)) {
                        availableModels.push(savedModel);
                    }
                });
            }

            this.postMessage({ 
                command: 'loadInitialData', 
                models: availableModels,
                specs: modelSpecs,
                config: configData
            });

            const tavilyKey = await this.context.secrets.get('tavily_api_key');
            const braveKey = await this.context.secrets.get('brave_api_key');

            this.postMessage({
                command: 'loadSecrets',
                secrets: {
                    tavily: tavilyKey ? '••••••••••••••••' : '',
                    brave: braveKey ? '••••••••••••••••' : ''
                }
            });
        } else if (message.command === 'updateSetting') {
            try {
                await this.udsClient.request('update_config', message.config);
                vscode.window.showInformationMessage('Settings successfully synced to Orchestrator!');
            } catch (error: any) {
                vscode.window.showErrorMessage(`Failed to sync settings: ${error.message}`);
            }
        } else if (message.command === 'scanModelsFromPath') {
            try {
                let targetPath = message.path || (message.data && message.data.path) || message.models_path;
                
                if (!targetPath) {
                    throw new Error("No path provided to scan.");
                }

                if (targetPath.startsWith('~')) {
                    const cleanPath = targetPath.startsWith('~/') ? targetPath.slice(2) : targetPath.slice(1);
                    targetPath = path.join(os.homedir(), cleanPath);
                }

                const models: string[] = [];
                const families = fs.readdirSync(targetPath, { withFileTypes: true });
                
                for (const family of families) {
                    if (family.isDirectory() && !family.name.startsWith('.')) {
                        const familyPath = path.join(targetPath, family.name);
                        const tags = fs.readdirSync(familyPath, { withFileTypes: true });
                        
                        for (const tag of tags) {
                            if (tag.isFile() && !tag.name.startsWith('.')) {
                                models.push(`${family.name}:${tag.name}`);
                            }
                        }
                    }
                }
                
                this.postMessage({ command: 'loadModels', models: models });
            } catch (error: any) {
                this.postMessage({ 
                    command: 'agentError', 
                    text: `Failed to scan directory: ${error.message}` 
                });
            }
        } else if (message.command === 'createProfile') {
            try {
                await this.udsClient.request('create_profile', {
                    profile_name: message.profileName,
                    profile_data: message.profileData
                });
                vscode.window.showInformationMessage(`Profile '${message.profileName}' created successfully!`);
                
                await new Promise(resolve => setTimeout(resolve, 100));
                const freshConfig = await this.udsClient.request('get_config', {});
                this.postMessage({ command: 'loadConfig', config: freshConfig });
            } catch (error: any) {
                vscode.window.showErrorMessage(`Failed to create profile: ${error.message}`);
            }
        } else if (message.command === 'deleteProfile') {
            try {
                await this.udsClient.request('delete_profile', { profile_name: message.profileName });
                vscode.window.showInformationMessage(`Profile '${message.profileName}' deleted.`);
                
                await new Promise(resolve => setTimeout(resolve, 100));
                const freshConfig = await this.udsClient.request('get_config', {});
                this.postMessage({ command: 'loadConfig', config: freshConfig });
            } catch (error: any) {
                vscode.window.showErrorMessage(`Failed to delete profile: ${error.message}`);
            }
        } else if (message.command === 'storeSecret') {
            await this.context.secrets.store(message.key, message.value);
        }
    }
}