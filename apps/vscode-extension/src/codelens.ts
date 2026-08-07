import * as vscode from 'vscode';

export class AgentApprovalCodeLensProvider implements vscode.CodeLensProvider {
    // We use an EventEmitter to tell VS Code to refresh the CodeLenses 
    // when a pending write request starts or stops.
    private _onDidChangeCodeLenses: vscode.EventEmitter<void> = new vscode.EventEmitter<void>();
    public readonly onDidChangeCodeLenses: vscode.Event<void> = this._onDidChangeCodeLenses.event;

    private isPendingApproval: boolean = false;

    public setPendingState(state: boolean) {
        this.isPendingApproval = state;
        this._onDidChangeCodeLenses.fire(); // Trigger UI redraw
    }

    public provideCodeLenses(
        document: vscode.TextDocument, 
        token: vscode.CancellationToken
    ): vscode.CodeLens[] | Thenable<vscode.CodeLens[]> {
        
        // Only show the lenses if we are actually waiting for an approval
        if (!this.isPendingApproval) {
            return [];
        }

        // Place the buttons at the very top of the file (Line 0)
        const topOfFile = new vscode.Range(0, 0, 0, 0);

        const approveLens = new vscode.CodeLens(topOfFile, {
            title: "$(check) Accept Change",
            command: "agenticWorkspace.approveWrite",
            tooltip: "Write these changes to disk"
        });

        const rejectLens = new vscode.CodeLens(topOfFile, {
            title: "$(close) Reject Change",
            command: "agenticWorkspace.rejectWrite",
            tooltip: "Discard these changes"
        });

        return [approveLens, rejectLens];
    }
}
