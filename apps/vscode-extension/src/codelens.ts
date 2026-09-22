import * as vscode from 'vscode';

export class AgentApprovalCodeLensProvider implements vscode.CodeLensProvider {
    private _onDidChangeCodeLenses: vscode.EventEmitter<void> = new vscode.EventEmitter<void>();
    public readonly onDidChangeCodeLenses: vscode.Event<void> = this._onDidChangeCodeLenses.event;

    // Map target lines by URI to prevent state collisions between multiple files
    private targetLines = new Map<string, number>();

    public setTargetLine(uri: vscode.Uri, line: number) {
        this.targetLines.set(uri.toString(), line);
        this._onDidChangeCodeLenses.fire(); 
    }

    public clearAll() {
        this.targetLines.clear();
        this._onDidChangeCodeLenses.fire();
    }

    public provideCodeLenses(
        document: vscode.TextDocument, 
        token: vscode.CancellationToken
    ): vscode.CodeLens[] {
        // IMPORTANT: Only return lenses if this specific URI is actively awaiting approval
        if (!this.targetLines.has(document.uri.toString())) {
            return [];
        }
        
        const requestedLine = this.targetLines.get(document.uri.toString()) || 0;
        
        // Safely bind the target line to the document's actual line count
        const safeLine = Math.max(0, Math.min(requestedLine, document.lineCount > 0 ? document.lineCount - 1 : 0));
        
        const targetRange = new vscode.Range(safeLine, 0, safeLine, 0);

        const approveLens = new vscode.CodeLens(targetRange, {
            title: "$(check) Accept Change",
            command: "agenticWorkspace.approveWrite",
            tooltip: "Write these changes to disk"
        });

        const rejectLens = new vscode.CodeLens(targetRange, {
            title: "$(close) Reject Change",
            command: "agenticWorkspace.rejectWrite",
            tooltip: "Discard these changes"
        });

        return [approveLens, rejectLens];
    }
}
