import { expect } from 'chai';
import { ChatViewProvider } from '../providers/ChatViewProvider';

describe('ChatViewProvider History & Session Fixes', () => {
    let mockUdsClient: any;
    let mockWebviewView: any;
    let postedMessages: any[];
    let onMessageCallback: (data: any) => Promise<void>;
    let provider: ChatViewProvider;
    
    let requestArgs: any[][] = [];

    beforeEach(() => {
        postedMessages = [];
        requestArgs = [];
        
        mockUdsClient = {
            on: () => {},
            request: async (...args: any[]) => {
                requestArgs.push(args);
                if (args[0] === 'fetch_history') {
                    return {
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
                    };
                }
                if (args[0] === 'execute_agent_task') {
                    return {
                        status: 'completed',
                        final_observation: 'Task complete',
                    };
                }
                return {};
            },
            cancelTask: () => {},
        };

        mockWebviewView = {
            webview: {
                options: {},
                html: '',
                asWebviewUri: (uri: any) => uri,
                postMessage: (msg: any) => postedMessages.push(msg),
                onDidReceiveMessage: (cb: any) => {
                    onMessageCallback = cb;
                },
            },
        };

        provider = new ChatViewProvider({ fsPath: '/mock/ext' } as any, mockUdsClient);
        provider.resolveWebviewView(mockWebviewView, {} as any, {} as any);
    });

    it('transforms raw SQLite ReAct trajectory into clean UI messages and filters intermediate tool noise (Bug #2)', async () => {
        await onMessageCallback({ command: 'fetchHistory', sessionId: 'sess-42' });

        expect(requestArgs).to.have.length.greaterThan(0);
        expect(requestArgs[0]).to.deep.equal(['fetch_history', { session_id: 'sess-42' }]);
        
        expect(postedMessages).to.have.lengthOf(1);
        expect(postedMessages[0].command).to.equal('historyData');

        const uiHistory = postedMessages[0].history;
        // Should contain: 1 user message, 2 thought messages, and ONLY 1 final agent response
        // Intermediate 'file_system' tool dump must be filtered out
        expect(uiHistory).to.deep.equal([
            { role: 'user', content: 'Why is New Chat empty?' },
            { role: 'thought', content: 'I will read ChatPanel.tsx to check handleNewChat.' },
            { role: 'thought', content: 'Found the bug, completing task.' },
            { role: 'agent', content: 'Fixed the session ID sync.' },
        ]);
    });

    it('forwards sessionId from executeTask to execute_agent_task UDS payload (Bug #3)', async () => {
        await onMessageCallback({
            command: 'executeTask',
            sessionId: 'custom-session-777',
            goal: 'Run tests',
            autoApprove: true,
            messages: [],
        });

        expect(requestArgs).to.have.length.greaterThan(0);
        expect(requestArgs[0][0]).to.equal('execute_agent_task');
        expect(requestArgs[0][1]).to.include({
            session_id: 'custom-session-777',
            goal: 'Run tests',
            auto_approve: true,
        });
    });
});