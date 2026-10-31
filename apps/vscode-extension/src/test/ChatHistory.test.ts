import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ChatViewProvider } from '../providers/ChatViewProvider';

// Mock vscode module
vi.mock('vscode', () => ({
    Uri: {
        joinPath: vi.fn(() => ({ fsPath: '/mock/path' })),
    },
    workspace: {
        workspaceFolders: [{ uri: { fsPath: '/mock/workspace' } }],
    },
    commands: {
        executeCommand: vi.fn(),
    },
}));

describe('ChatViewProvider History & Session Fixes', () => {
    let mockUdsClient: any;
    let mockWebviewView: any;
    let postedMessages: any[];
    let onMessageCallback: (data: any) => Promise<void>;
    let provider: ChatViewProvider;

    beforeEach(() => {
        postedMessages = [];
        mockUdsClient = {
            on: vi.fn(),
            request: vi.fn(),
            cancelTask: vi.fn(),
        };

        mockWebviewView = {
            webview: {
                options: {},
                html: '',
                asWebviewUri: vi.fn((uri) => uri),
                postMessage: vi.fn((msg) => postedMessages.push(msg)),
                onDidReceiveMessage: vi.fn((cb) => {
                    onMessageCallback = cb;
                }),
            },
        };

        provider = new ChatViewProvider({ fsPath: '/mock/ext' } as any, mockUdsClient);
        provider.resolveWebviewView(mockWebviewView, {} as any, {} as any);
    });

    it('transforms raw SQLite ReAct trajectory into clean UI messages and filters intermediate tool noise (Bug #2)', async () => {
        // Simulate raw SQLite rows returned by uds_server.py fetch_history
        mockUdsClient.request.mockResolvedValueOnce({
            status: 'success',
            history: [
                {
                    role: 'user',
                    content: JSON.stringify('Why is New Chat empty?'),
                },
                {
                    role: 'assistant',
                    content: JSON.stringify(JSON.stringify({
                        reasoning: 'I will read ChatPanel.tsx to check handleNewChat.',
                        tool: 'file_system',
                        tool_args: { action: 'read', path: 'ChatPanel.tsx' },
                    })),
                },
                {
                    role: 'tool',
                    content: JSON.stringify('File content of ChatPanel.tsx: import React...'),
                },
                {
                    role: 'assistant',
                    content: JSON.stringify(JSON.stringify({
                        reasoning: 'Found the bug, completing task.',
                        tool: 'finish_task',
                        tool_args: { summary: 'Fixed the session ID sync.', success: true },
                    })),
                },
                {
                    role: 'tool',
                    content: JSON.stringify('Fixed the session ID sync.'),
                },
            ],
        });

        await onMessageCallback({ command: 'fetchHistory', sessionId: 'sess-42' });

        expect(mockUdsClient.request).toHaveBeenCalledWith('fetch_history', { session_id: 'sess-42' });
        expect(postedMessages).toHaveLength(1);
        expect(postedMessages[0].command).toBe('historyData');

        const uiHistory = postedMessages[0].history;
        // Should contain: 1 user message, 2 thought messages, and ONLY 1 final agent response
        // Intermediate 'file_system' tool dump must be filtered out!
        expect(uiHistory).toEqual([
            { role: 'user', content: 'Why is New Chat empty?' },
            { role: 'thought', content: 'I will read ChatPanel.tsx to check handleNewChat.' },
            { role: 'thought', content: 'Found the bug, completing task.' },
            { role: 'agent', content: 'Fixed the session ID sync.' },
        ]);
    });

    it('forwards sessionId from executeTask to execute_agent_task UDS payload (Bug #3)', async () => {
        mockUdsClient.request.mockResolvedValueOnce({
            status: 'completed',
            final_observation: 'Task complete',
        });

        await onMessageCallback({
            command: 'executeTask',
            sessionId: 'custom-session-777',
            goal: 'Run tests',
            autoApprove: true,
            messages: [],
        });

        expect(mockUdsClient.request).toHaveBeenCalledWith(
            'execute_agent_task',
            expect.objectContaining({
                session_id: 'custom-session-777',
                goal: 'Run tests',
                auto_approve: true,
            })
        );
    });
});