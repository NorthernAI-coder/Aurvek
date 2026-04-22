(function() {
    'use strict';

    const TYPING_DELAY_MS = 3500;
    const ATTACHMENT_DELAY_MS = 250;
    const COOLDOWN_MS = 15000;

    let typingTimer = null;
    let attachmentTimer = null;
    let activeConversationId = null;
    const lastSentAt = new Map();

    function debug(...args) {
        if (window.console && typeof window.console.debug === 'function') {
            console.debug('[ChatWarmup]', ...args);
        }
    }

    function getConversationId() {
        if (typeof window.currentConversationId !== 'undefined' && window.currentConversationId !== null) {
            return window.currentConversationId;
        }
        if (typeof currentConversationId !== 'undefined' && currentConversationId !== null) {
            return currentConversationId;
        }
        return null;
    }

    function getDraftLength() {
        const textarea = document.getElementById('message-text');
        return textarea && textarea.value ? textarea.value.trim().length : 0;
    }

    function normalizeKind(mimeType, filename) {
        const mime = (mimeType || '').toLowerCase();
        const name = (filename || '').toLowerCase();

        if (mime.startsWith('image/')) {
            return 'image';
        }
        if (mime === 'application/pdf' || name.endsWith('.pdf')) {
            return 'pdf';
        }
        if (mime.startsWith('text/') || /\.(txt|md|csv|json|xml|html|css|js|ts|py|log)$/.test(name)) {
            return 'text';
        }
        if (mime.startsWith('audio/')) {
            return 'audio';
        }
        if (mime.startsWith('video/')) {
            return 'video';
        }
        return 'file';
    }

    function summarizeFiles(files) {
        const list = Array.from(files || []).filter(Boolean);
        if (!list.length) {
            return { has_attachments: false, attachment_kinds: [], attachment_count: 0 };
        }

        const kinds = [];
        list.forEach((file) => {
            const kind = normalizeKind(file.type, file.name);
            if (!kinds.includes(kind)) {
                kinds.push(kind);
            }
        });

        return {
            has_attachments: true,
            attachment_kinds: kinds,
            attachment_count: list.length,
        };
    }

    function summarizeItems(items) {
        const files = [];
        Array.from(items || []).forEach((item) => {
            if (item && item.kind === 'file') {
                files.push({
                    type: item.type || '',
                    name: '',
                });
            }
        });
        return summarizeFiles(files);
    }

    function getAttachedFilesSummary() {
        if (typeof attachedFiles !== 'undefined' && Array.isArray(attachedFiles) && attachedFiles.length) {
            return summarizeFiles(attachedFiles);
        }
        if (Array.isArray(window.attachedFiles) && window.attachedFiles.length) {
            return summarizeFiles(window.attachedFiles);
        }
        return { has_attachments: false, attachment_kinds: [], attachment_count: 0 };
    }

    function getSelectedMultiAiModelIds() {
        const manager = window.multiAiManager;
        if (!manager || !manager.enabled || !Array.isArray(manager.selectedModels)) {
            return [];
        }

        return manager.selectedModels
            .map((model) => Number(model.llm_id || model.id || model.model_id))
            .filter((modelId) => Number.isInteger(modelId) && modelId > 0);
    }

    function getLastKnownMessageId() {
        let maxId = 0;
        document.querySelectorAll('#chat-messages-container .message[data-message-id], .message[data-message-id]')
            .forEach((element) => {
                const id = Number.parseInt(element.getAttribute('data-message-id'), 10);
                if (Number.isInteger(id) && id > maxId) {
                    maxId = id;
                }
            });
        return maxId;
    }

    function cancelTimers() {
        if (typingTimer) {
            clearTimeout(typingTimer);
            typingTimer = null;
        }
        if (attachmentTimer) {
            clearTimeout(attachmentTimer);
            attachmentTimer = null;
        }
    }

    function clearCooldown(conversationId) {
        const targetConversationId = conversationId || getConversationId();
        if (targetConversationId) {
            lastSentAt.delete(String(targetConversationId));
        }
    }

    function cancelActiveWarmup() {
        cancelTimers();
        clearCooldown();
    }

    function buildPayload(activity, metadata) {
        const attachedSummary = getAttachedFilesSummary();
        const metadataKinds = metadata.attachment_kinds || metadata.attachmentKinds || [];
        const attachmentKinds = metadataKinds.length ? metadataKinds : attachedSummary.attachment_kinds;
        const hasAttachments = Boolean(
            metadata.has_attachments ||
            metadata.hasAttachments ||
            attachedSummary.has_attachments ||
            attachmentKinds.length
        );

        return {
            activity,
            draft_length: Number.isInteger(metadata.draft_length)
                ? metadata.draft_length
                : Number.isInteger(metadata.draftLength)
                    ? metadata.draftLength
                    : getDraftLength(),
            has_attachments: hasAttachments,
            attachment_kinds: attachmentKinds,
            multi_ai_model_ids: getSelectedMultiAiModelIds(),
            last_known_message_id: getLastKnownMessageId(),
        };
    }

    function sendWarmup(activity, metadata) {
        const conversationId = getConversationId();
        if (!conversationId) {
            return;
        }

        const payload = buildPayload(activity, metadata || {});
        if (activity === 'typing' && payload.draft_length <= 0) {
            return;
        }

        const cooldownKey = String(conversationId);
        const now = Date.now();
        const previous = lastSentAt.get(cooldownKey) || 0;
        if (now - previous < COOLDOWN_MS) {
            debug('cooldown', { conversationId, activity });
            return;
        }
        lastSentAt.set(cooldownKey, now);

        const fetchFn = typeof secureFetch === 'function' ? secureFetch : fetch;
        fetchFn(`/api/conversations/${conversationId}/warmup`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
            .then((response) => {
                if (!response.ok) {
                    debug('request skipped', response.status);
                    return null;
                }
                return response.json().catch(() => null);
            })
            .then((data) => {
                if (data) {
                    debug('prepared', data.status, data);
                }
            })
            .catch((error) => {
                debug('request failed', error);
            });
    }

    function signal(activity, metadata) {
        const conversationId = getConversationId();
        if (!conversationId) {
            return;
        }

        if (activeConversationId !== null && String(activeConversationId) !== String(conversationId)) {
            resetForConversation(conversationId);
        }
        activeConversationId = conversationId;

        if (activity === 'typing') {
            const draftLength = Number.isInteger(metadata && metadata.draftLength)
                ? metadata.draftLength
                : getDraftLength();
            if (draftLength <= 0) {
                cancelActiveWarmup();
                return;
            }

            if (typingTimer) {
                clearTimeout(typingTimer);
            }
            typingTimer = setTimeout(() => {
                typingTimer = null;
                sendWarmup('typing', { ...(metadata || {}), draftLength });
            }, TYPING_DELAY_MS);
            return;
        }

        if (activity === 'attachment') {
            if (attachmentTimer) {
                clearTimeout(attachmentTimer);
            }
            attachmentTimer = setTimeout(() => {
                attachmentTimer = null;
                sendWarmup('attachment', metadata || {});
            }, ATTACHMENT_DELAY_MS);
            return;
        }

        if (activity === 'audio_recording' || activity === 'voice_call') {
            sendWarmup(activity, metadata || {});
        }
    }

    function resetForConversation(conversationId) {
        cancelTimers();
        activeConversationId = conversationId || null;
    }

    function bindDomEvents() {
        const textarea = document.getElementById('message-text');
        if (textarea) {
            textarea.addEventListener('input', () => {
                signal('typing', { draftLength: getDraftLength() });
            });
        }

        const form = document.getElementById('form-message');
        if (form) {
            form.addEventListener('submit', cancelActiveWarmup);
        }

        const fileInput = document.getElementById('chat-files');
        if (fileInput) {
            fileInput.addEventListener('change', () => {
                signal('attachment', summarizeFiles(fileInput.files));
            });
        }

        document.addEventListener('paste', (event) => {
            const summary = summarizeItems(event.clipboardData && event.clipboardData.items);
            if (summary.has_attachments) {
                signal('attachment', summary);
            }
        }, true);

        document.addEventListener('drop', (event) => {
            const summary = summarizeFiles(event.dataTransfer && event.dataTransfer.files);
            if (summary.has_attachments) {
                signal('attachment', summary);
            }
        }, true);
    }

    window.ChatWarmup = {
        signal,
        cancel: cancelActiveWarmup,
        resetForConversation,
    };

    document.addEventListener('DOMContentLoaded', bindDomEvents);
})();
