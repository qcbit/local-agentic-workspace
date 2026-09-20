import * as vscode from 'vscode';

export class AgentApprovalCodeLensProvider implements vscode.CodeLensProvider {
    // We use an EventEmitter to tell VS Code to refresh the CodeLenses 
    // when a pending write request starts or stops.
    private _onDidChangeCodeLenses: vscode.EventEmitter<void> = new vscode.EventEmitter<void>();
    public readonly onDidChangeCodeLenses: vscode.Event<void> = this._onDidChangeCodeLenses.event;

    private isPendingApproval: boolean = false;
    private targetLine: number = 0; // Default to top of file

    // Accept the specific line where the first change occurs
    public setPendingState(state: boolean, line: number = 0) {
        this.isPendingApproval = state;
        this.targetLine = line;
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

        // Anchor to the dynamic target line instead of 0
        const targetRange = new vscode.Range(this.targetLine, 0, this.targetLine, 0);

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
