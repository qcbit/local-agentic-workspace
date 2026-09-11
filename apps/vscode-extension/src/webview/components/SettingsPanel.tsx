import React, { useState, useEffect } from 'react';

// Safely acquire and cache the VS Code API
declare const acquireVsCodeApi: any;
const vscode = (window as any).vscodeApi || ((window as any).vscodeApi = acquireVsCodeApi());

const TOKEN_PRESETS = [4000, 6000, 8000, 16000, 24000, 32000, 64000, 128000];
const DEFAULT_OLLAMA_PATH = '~/.ollama/models/manifests/registry.ollama.ai/library';

type TabType = 'general' | 'search';

export const SettingsPanel: React.FC = () => {
    const [isLoading, setIsLoading] = useState(true);
    const [activeTab, setActiveTab] = useState<TabType>('general');
    
    // Profile & LLM Settings
    const [config, setConfig] = useState<any>(null);
    const [activeProfile, setActiveProfile] = useState<string>('Default');
    const [profiles, setProfiles] = useState<Record<string, any>>({});
    const [newProfileName, setNewProfileName] = useState<string>('');
    const [endpointUrl, setEndpointUrl] = useState('');
    const [apiKey, setApiKey] = useState('');

    // Models & Memory
    const [modelName, setModelName] = useState(''); 
    const [modelsPath, setModelsPath] = useState(DEFAULT_OLLAMA_PATH);
    const [tokenLimit, setTokenLimit] = useState(6000);
    const [availableModels, setAvailableModels] = useState<string[]>([]);
    const [modelSpecs, setModelSpecs] = useState<Record<string, { recommendedMax: number }>>({});

    // Token Heuristics
    const [charsPerToken, setCharsPerToken] = useState<number>(4.0);

    // Sandbox Security
    const [strictMode, setStrictMode] = useState(true);
    const [allowedExternalPaths, setAllowedExternalPaths] = useState('');

    // Search Provider Settings (Tiered Strategy)
    const [tavilyApiKey, setTavilyApiKey] = useState('');
    const [braveApiKey, setBraveApiKey] = useState('');
    const [searxngUrl, setSearxngUrl] = useState('http://localhost:8080');
    const [searchEnabled, setSearchEnabled] = useState(true);

    const applyProfileState = (profileName: string, targetConfig: any) => {
        const profile = targetConfig?.profiles?.[profileName];
        if (profile) {
            setEndpointUrl(profile.llm?.endpoint_url || profile.llm?.endpoint || '');
            setApiKey(profile.llm?.api_key || profile.llm?.apiKey || '');
            setModelName(profile.llm?.model_name || profile.llm?.model || '');
            setModelsPath(profile.llm?.models_path || DEFAULT_OLLAMA_PATH);
            setTokenLimit(profile.memory?.max_tokens || 6000);
            setCharsPerToken(profile.llm?.chars_per_token ?? 4.0);
            setStrictMode(profile.sandbox?.strict_mode ?? true);
            setAllowedExternalPaths((profile.sandbox?.allowed_external_paths || []).join('\n'));

            // Search config
            setSearxngUrl(profile.search?.searxng_url || 'http://localhost:8080');
            setSearchEnabled(profile.search?.enabled ?? true);
        } else {
            setEndpointUrl('http://127.0.0.1:11434/v1/chat/completions');
            setApiKey('');
            setModelName('llama3:8b');
            setModelsPath(DEFAULT_OLLAMA_PATH);
            setTokenLimit(6000);
            setCharsPerToken(4.0);
            setStrictMode(true);
            setAllowedExternalPaths('');
            setSearxngUrl('http://localhost:8080');
            setSearchEnabled(true);
        }
    };

    useEffect(() => {
        const handler = (event: MessageEvent) => {
            const message = event.data;
            
            if ((message.command === 'loadConfig' || message.command === 'loadInitialData') && message.config) {
                const cfg = message.config;
                setConfig(cfg);
                const active = cfg.active_profile || 'Default';
                setActiveProfile(active);
                setProfiles(cfg.profiles || {});
                
                applyProfileState(active, cfg);

                // Populate injected models and specs
                if (message.models) setAvailableModels(message.models);
                if (message.specs) setModelSpecs(message.specs);

                setIsLoading(false);
            }

            if (message.command === 'loadModels') {
                if (message.models) setAvailableModels(message.models);
                if (message.specs) setModelSpecs(message.specs);
            }

            // Masked indicator updates from SecretStorage
            if (message.command === 'loadSecrets') {
                if (message.secrets?.tavily) setTavilyApiKey(message.secrets.tavily);
                if (message.secrets?.brave) setBraveApiKey(message.secrets.brave);
            }
        };

        window.addEventListener('message', handler);
        vscode.postMessage({ command: 'ready' });
        
        return () => window.removeEventListener('message', handler);
    }, []);

    const handleProfileChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
        const selected = e.target.value;
        setActiveProfile(selected);
        applyProfileState(selected, { profiles });
    };

    const handleCreateProfile = () => {
        if (!newProfileName.trim() || profiles[newProfileName]) return;
        const defaultProfileData = {
            llm: { 
                endpoint_url: "http://127.0.0.1:11434/v1/chat/completions", 
                model_name: "llama3:8b", 
                api_key: "", 
                chars_per_token: 4.0 
            },
            memory: { max_tokens: 6000 },
            sandbox: { strict_mode: true, allowed_external_paths: [] },
            search: { enabled: true, searxng_url: "http://localhost:8080" }
        };
        vscode.postMessage({
            command: 'createProfile',
            profileName: newProfileName.trim(),
            profileData: defaultProfileData
        });
        setNewProfileName('');
    };

    const handleDeleteProfile = (name: string) => {
        if (Object.keys(profiles).length <= 1) return; 
        vscode.postMessage({
            command: 'deleteProfile',
            profileName: name
        });
    };

    const handleModelChange = (val: string) => {
        setModelName(val);
        if (modelSpecs && val) {
            const matchedKey = Object.keys(modelSpecs).find(key => val.includes(key));
            if (matchedKey && modelSpecs[matchedKey]?.recommendedMax) {
                setTokenLimit(modelSpecs[matchedKey].recommendedMax);
            }
        }
    };

    const handleScanModels = () => {
        vscode.postMessage({ command: 'scanModelsFromPath', path: modelsPath });
    };

    const handleSync = () => {
        // 1. Sync config object to Orchestrator (.agentic_config.json)
        vscode.postMessage({
            command: 'updateSetting',
            config: {
                active_profile: activeProfile,
                profile_settings: {
                    llm: { 
                        endpoint_url: endpointUrl,
                        api_key: apiKey,
                        model_name: modelName,
                        models_path: modelsPath,
                        chars_per_token: charsPerToken
                    },
                    memory: { 
                        max_tokens: tokenLimit
                    },
                    sandbox: {
                        strict_mode: strictMode,
                        allowed_external_paths: allowedExternalPaths.split('\n').map(p => p.trim()).filter(p => p !== '')
                    },
                    search: {
                        enabled: searchEnabled,
                        searxng_url: searxngUrl
                    }
                }
            }
        });

        // 2. Persist API keys via SecretStorage IPC (Keychain)
        if (tavilyApiKey && !tavilyApiKey.includes('••••')) {
            vscode.postMessage({
                command: 'storeSecret',
                key: 'tavily_api_key',
                value: tavilyApiKey
            });
        }

        if (braveApiKey && !braveApiKey.includes('••••')) {
            vscode.postMessage({
                command: 'storeSecret',
                key: 'brave_api_key',
                value: braveApiKey
            });
        }
    };

    if (isLoading) {
        return (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100vh', gap: '1rem', color: 'var(--vscode-foreground)' }}>
                <div style={{ border: '4px solid var(--vscode-widget-border)', borderTop: '4px solid var(--vscode-button-background)', borderRadius: '50%', width: '40px', height: '40px', animation: 'spin 1s linear infinite' }}></div>
                <style>{`@keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }`}</style>
                <h3>Fetching Workspace Environment...</h3>
            </div>
        );
    }

    const inputStyle: React.CSSProperties = {
        padding: '0.5rem',
        marginTop: '0.25rem',
        backgroundColor: 'var(--vscode-input-background)',
        color: 'var(--vscode-input-foreground)',
        border: '1px solid var(--vscode-input-border)'
    };

    const tabStyle = (tab: TabType): React.CSSProperties => ({
        padding: '0.5rem 1rem',
        cursor: 'pointer',
        border: 'none',
        background: activeTab === tab ? 'var(--vscode-tab-activeBackground)' : 'var(--vscode-tab-inactiveBackground)',
        color: activeTab === tab ? 'var(--vscode-tab-activeForeground)' : 'var(--vscode-tab-inactiveForeground)',
        borderBottom: activeTab === tab ? '2px solid var(--vscode-focusBorder)' : 'none',
        fontWeight: activeTab === tab ? 'bold' : 'normal'
    });

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', padding: '1rem' }}>
            <h2>Agentic Workspace Settings</h2>
            
            <div style={{ marginBottom: '15px' }}>
                <label><strong>Active Profile: </strong></label>
                <select value={activeProfile} onChange={handleProfileChange} style={{ padding: '5px', marginLeft: '10px' }}>
                    {Object.keys(profiles).map(p => (
                        <option key={p} value={p}>{p}</option>
                    ))}
                </select>
                {activeProfile !== 'Default' && (
                    <button onClick={() => handleDeleteProfile(activeProfile)} style={{ marginLeft: '10px', background: '#d32f2f', color: 'white', border: 'none', padding: '5px 10px', cursor: 'pointer' }}>
                        Delete Profile
                    </button>
                )}
            </div>

            <div style={{ display: 'flex', gap: '10px', marginBottom: '10px' }}>
                <input 
                    type="text" 
                    placeholder="New profile name..." 
                    value={newProfileName} 
                    onChange={e => setNewProfileName(e.target.value)}
                    style={{ padding: '5px' }}
                />
                <button onClick={handleCreateProfile}>Add Profile</button>
            </div>

            {/* Tab Navigation */}
            <div style={{ display: 'flex', gap: '4px', borderBottom: '1px solid var(--vscode-widget-border)' }}>
                <button style={tabStyle('general')} onClick={() => setActiveTab('general')}>
                    Model & Sandbox
                </button>
                <button style={tabStyle('search')} onClick={() => setActiveTab('search')}>
                    Search Providers
                </button>
            </div>

            {activeTab === 'general' && (
                <>
                    <h3>{activeProfile.toUpperCase()} Profile Settings</h3>

                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <label>Endpoint URL</label>
                        <input type="text" value={endpointUrl} onChange={(e) => setEndpointUrl(e.target.value)} style={inputStyle} />
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <label>API Key / Auth Token</label>
                        <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} style={inputStyle} />
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <label>Local Models Path (For Dropdown)</label>
                        <div style={{ display: 'flex', gap: '0.5rem' }}>
                            <input 
                                type="text"
                                value={modelsPath}
                                onChange={(e) => setModelsPath(e.target.value)}
                                placeholder="e.g. ~/.ollama/models/manifests/registry.ollama.ai/library"
                                style={{ ...inputStyle, flexGrow: 1 }}
                            />
                            <button 
                                onClick={handleScanModels}
                                style={{
                                    padding: '0.5rem 1rem',
                                    marginTop: '0.25rem',
                                    backgroundColor: 'var(--vscode-button-secondaryBackground)',
                                    color: 'var(--vscode-button-secondaryForeground)',
                                    border: 'none',
                                    cursor: 'pointer'
                                }}
                            >
                                Scan Directory
                            </button>
                        </div>
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <label>Model Deployment Name</label>
                        <input type="text" list="available-models-list" value={modelName} onChange={(e) => handleModelChange(e.target.value)} style={inputStyle} />
                        <datalist id="available-models-list">
                            {availableModels?.map(model => (
                                <option key={model} value={model} />
                            ))}
                        </datalist>
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <label>Max Context Tokens</label>
                        <input type="number" list="token-presets-list" value={tokenLimit} onChange={(e) => setTokenLimit(Number(e.target.value))} style={inputStyle} />
                        <datalist id="token-presets-list">
                            {TOKEN_PRESETS.map(preset => (
                                <option key={preset} value={preset} />
                            ))}
                        </datalist>
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                            <label>Characters per Token</label>
                            <span 
                                title="Rule of thumb: 1 token ≈ 4 characters for English text."
                                style={{
                                    cursor: 'help',
                                    borderRadius: '50%',
                                    background: 'var(--vscode-badge-background)',
                                    color: 'var(--vscode-badge-foreground)',
                                    width: '16px',
                                    height: '16px',
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'center',
                                    fontSize: '11px',
                                    fontWeight: 'bold'
                                }}
                            >
                                ?
                            </span>
                        </div>
                        <input 
                            type="number"
                            step="0.1"
                            value={charsPerToken}
                            onChange={(e) => setCharsPerToken(parseFloat(e.target.value))}
                            style={inputStyle}
                        />
                    </div>

                    <hr style={{ width: '100%', borderColor: 'var(--vscode-widget-border)' }} />
                    
                    <h3>Sandbox Security</h3>

                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
                        <input 
                            type="checkbox" 
                            checked={strictMode}
                            onChange={(e) => setStrictMode(e.target.checked)}
                        />
                        <label>Enable Strict Mode (Enforce Workspace Boundaries)</label>
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <label>Allowed External Paths (One per line)</label>
                        <textarea 
                            rows={4}
                            value={allowedExternalPaths}
                            onChange={(e) => setAllowedExternalPaths(e.target.value)}
                            disabled={!strictMode}
                            placeholder={"~/.ollama/models\n/Users/shared/libraries"}
                            style={{ ...inputStyle, fontFamily: 'monospace', resize: 'vertical' }}
                        />
                    </div>
                </>
            )}

            {activeTab === 'search' && (
                <>
                    <h3>Tiered Web Search Providers</h3>
                    <p style={{ fontSize: '12px', color: 'var(--vscode-descriptionForeground)' }}>
                        Search requests execute via a resilient waterfall: Tavily (Tier 1) &rarr; Brave (Tier 2) &rarr; SearxNG (Tier 3). API keys are encrypted securely in your OS Keychain via VS Code SecretStorage.
                    </p>

                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
                        <input 
                            type="checkbox" 
                            checked={searchEnabled}
                            onChange={(e) => setSearchEnabled(e.target.checked)}
                        />
                        <label>Enable Web Search Tool</label>
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <label>Tier 1: Tavily API Key (Primary)</label>
                        <input 
                            type="password" 
                            placeholder="tvly-..."
                            value={tavilyApiKey} 
                            onChange={(e) => setTavilyApiKey(e.target.value)} 
                            style={inputStyle} 
                        />
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <label>Tier 2: Brave Search API Key (Fallback)</label>
                        <input 
                            type="password" 
                            placeholder="BSA..."
                            value={braveApiKey} 
                            onChange={(e) => setBraveApiKey(e.target.value)} 
                            style={inputStyle} 
                        />
                    </div>

                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <label>Tier 3: SearxNG Instance URL (Local Failsafe)</label>
                        <input 
                            type="text" 
                            placeholder="http://localhost:8080"
                            value={searxngUrl} 
                            onChange={(e) => setSearxngUrl(e.target.value)} 
                            style={inputStyle} 
                        />
                    </div>
                </>
            )}

            <button onClick={handleSync} style={{ padding: '0.6rem 1rem', cursor: 'pointer', marginTop: '0.5rem', backgroundColor: 'var(--vscode-button-background)', color: 'var(--vscode-button-foreground)', border: 'none', fontWeight: 'bold' }}>
                Save & Sync to Orchestrator
            </button>
        </div>
    );
};
