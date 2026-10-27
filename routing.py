"""Audited, embedded domain routing baseline; see RULE-SOURCES.md.

No runtime remote rule downloads, scripts, MITM, or third-party converters.
Domain facts are deliberately scoped: not all Microsoft/Google/GitHub traffic
belongs to AI, and shared cloud IP ranges must not be assigned to a service.
"""

CLAUDE = ('anthropic.com', 'claude.ai', 'claude.com', 'claudeusercontent.com', 'clau.de')
AI = (
    'openai.com', 'chatgpt.com', 'oaistatic.com', 'oaiusercontent.com',
    'chatgpt.livekit.cloud', 'openaiapi-site.azureedge.net',
    'gemini.google.com', 'aistudio.google.com', 'ai.google.dev',
    'makersuite.google.com', 'generativelanguage.googleapis.com',
    'alkalimakersuite-pa.clients6.google.com', 'aistudio-pa.googleapis.com',
    'proactivebackend-pa.googleapis.com', 'notebooklm.google.com', 'notebooklm.google',
    'deepmind.com', 'deepmind.google', 'generativeai.google',
    'copilot.microsoft.com', 'copilot.cloud.microsoft', 'githubcopilot.com',
    'githubcopilot.net', 'copilot-proxy.githubusercontent.com',
    'x.ai', 'grok.com', 'perplexity.ai', 'perplexity.com', 'pplx.ai',
    'cursor.com', 'cursor.sh', 'augmentcode.com', 'windsurf.com',
    'codeium.com', 'codeiumdata.com', 'trae.ai', 'trae-api-sg.mchost.guru',
)
YOUTUBE = (
    'youtube.com', 'youtu.be', 'youtube-nocookie.com', 'googlevideo.com',
    'ytimg.com', 'ggpht.com', 'ggpht.cn', 'yt.be', 'youtubei.googleapis.com',
    'youtube.googleapis.com', 'youtubeembeddedplayer.googleapis.com',
    'youtubekids.com', 'youtubeeducation.com', 'withyoutube.com',
)
TIKTOK = (
    'tiktok.com', 'tiktokv.com', 'tiktokv.us', 'tiktokcdn.com', 'tiktokcdn-us.com',
    'tiktokd.com', 'tiktokd.org', 'tik-tokapi.com', 'tiktokshop.com',
    'tiktokglobalshop.com', 'tiktokmusic.app', 'musical.ly', 'muscdn.com',
    'byteoversea.com', 'byteintlapi.com', 'ibytedtos.com', 'ibyteimg.com',
    'ipstatp.com', 'isnssdk.com', 'sgpstatp.com', 'snssdk.com',
    'bytedapm.com', 'bytegecko-i18n.com', 'ttwebview.com',
)
SCENES = {'Claude': CLAUDE, 'AI': AI, 'YouTube': YOUTUBE, 'TikTok': TIKTOK}
EXACT = {'AI': ('openai-api.arkoselabs.com', 'chat.openai.com.cdn.cloudflare.net'),
         'TikTok': ('p16-tiktokcdn-com.akamaized.net', 'lf16-effectcdn.byteeffecttos-g.com',
                    'lf16-pkgcdn.pitaya-clientai.com')}
KEYWORDS = {'YouTube': ('youtube',), 'TikTok': ('tiktok', 'musical.ly')}


def scene_rules():
    result = []
    for group, domains in SCENES.items():
        result += [f'DOMAIN-SUFFIX,{d},{group}' for d in domains]
        result += [f'DOMAIN,{d},{group}' for d in EXACT.get(group, ())]
        result += [f'DOMAIN-KEYWORD,{d},{group}' for d in KEYWORDS.get(group, ())]
    return result


def dns_policies():
    result = {}
    for group, domains in SCENES.items():
        result.update({'+.' + d: 'https://1.1.1.1/dns-query#' + group for d in domains})
        result.update({d: 'https://1.1.1.1/dns-query#' + group for d in EXACT.get(group, ())})
    return result
