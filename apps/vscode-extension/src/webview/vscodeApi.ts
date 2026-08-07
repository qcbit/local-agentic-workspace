// src/webview/vscodeApi.ts

// Tell TypeScript that this VS Code global exists
declare const acquireVsCodeApi: any;

class VSCodeAPIWrapper {
    private readonly vsCodeApi: any;

    constructor() {
        // Check if we are actually running inside VS Code
        if (typeof acquireVsCodeApi === 'function') {
            this.vsCodeApi = acquireVsCodeApi();
        }
    }

    /**
     * Post a message to the extension backend
     */
    public postMessage(message: any) {
        if (this.vsCodeApi) {
            this.vsCodeApi.postMessage(message);
        } else {
            console.warn('VS Code API not found. Message not sent:', message);
        }
    }

    /**
     * Get the persistent state
     */
    public getState() {
        if (this.vsCodeApi) {
            return this.vsCodeApi.getState();
        }
        return undefined;
    }

    /**
     * Set the persistent state
     */
    public setState(newState: any) {
        if (this.vsCodeApi) {
            this.vsCodeApi.setState(newState);
        }
    }
}

// Export a single, cached instance
export const vscode = new VSCodeAPIWrapper();
