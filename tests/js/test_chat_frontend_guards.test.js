const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..', '..');
const chatPath = path.join(repoRoot, 'data/static/js/chat/chat.js');
const fileHandlingPath = path.join(repoRoot, 'data/static/js/chat/fileHandling.js');

function extract(source, startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert.notEqual(start, -1, `Missing marker: ${startMarker}`);
    assert.notEqual(end, -1, `Missing marker: ${endMarker}`);
    return source.slice(start, end);
}

function classList() {
    const values = new Set();
    return {
        add(...names) { names.forEach(name => values.add(name)); },
        contains(name) { return values.has(name); },
    };
}

class FakeElement {
    constructor(tagName) {
        this.tagName = tagName;
        this.children = [];
        this.classList = classList();
        this.className = '';
        this.style = {};
        this.dataset = {};
        this.textContent = '';
        this.title = '';
        this.firstChild = null;
        this.innerHTMLWrites = [];
    }

    set innerHTML(value) {
        this.innerHTMLWrites.push(value);
        this.children = [];
        this.firstChild = null;
    }

    get innerHTML() { return ''; }

    appendChild(child) {
        this.children.push(child);
        this.firstChild = this.children[0] || null;
        return child;
    }

    insertBefore(child) {
        this.children.unshift(child);
        this.firstChild = this.children[0] || null;
        return child;
    }

    querySelector(selector) {
        if (selector === '.prompt-info') {
            return this.children.find(child => child.classList.contains('prompt-info')) || null;
        }
        return null;
    }

    remove() {}
}

function findByClass(root, name) {
    if (root.classList?.contains(name) || root.className.split(/\s+/).includes(name)) {
        return root;
    }
    for (const child of root.children || []) {
        const match = findByClass(child, name);
        if (match) return match;
    }
    return null;
}

test('prompt extension names are rendered as text instead of HTML', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const showPromptInfoSource = extract(
        source,
        'function showPromptInfo()',
        '// Model Selector functionality'
    );
    const chatMessagesContainer = new FakeElement('div');
    const maliciousName = '<img src=x onerror="globalThis.pwned=true">';
    const document = {
        createElement(tagName) { return new FakeElement(tagName); },
        getElementById(id) {
            return id === 'chat-messages-container' ? chatMessagesContainer : null;
        },
    };
    const context = {
        document,
        window: {
            extensionSelector: {
                extensions: [{ id: 1, name: maliciousName }],
                currentExtensionId: 1,
            },
        },
        botname: 'Assistant',
        promptDescription: 'Safe description',
        botProfilePicture: '',
        botProfilePicture128: '',
        botProfilePictureFullsize: '',
        imageHandler: { showFullsize() {} },
        String,
    };
    vm.createContext(context);
    vm.runInContext(showPromptInfoSource, context);
    vm.runInContext('showPromptInfo()', context);

    const pill = findByClass(chatMessagesContainer, 'extension-pill');
    assert.ok(pill);
    assert.equal(pill.textContent, maliciousName);
    assert.equal(context.pwned, undefined);
    assert.deepEqual(pill.innerHTMLWrites, []);
});

test('bot avatars keep their exact signed URLs for prompt info and voice calls', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const showPromptInfoSource = extract(
        source,
        'function showPromptInfo()',
        '// Model Selector functionality'
    );
    const chatMessagesContainer = new FakeElement('div');
    const openedUrls = [];
    const signed32 = '/avatar_32.webp?token=signed-for-32';
    const signed128 = '/avatar_128.webp?token=signed-for-128';
    const signedFullsize = '/avatar_fullsize.webp?token=signed-for-fullsize';
    const context = {
        document: {
            createElement(tagName) { return new FakeElement(tagName); },
            getElementById(id) {
                return id === 'chat-messages-container' ? chatMessagesContainer : null;
            },
        },
        window: {},
        botname: 'Assistant',
        promptDescription: 'Description',
        botProfilePicture: signed32,
        botProfilePicture128: signed128,
        botProfilePictureFullsize: signedFullsize,
        imageHandler: {
            showFullsize(url) { openedUrls.push(url); },
        },
        String,
    };
    vm.createContext(context);
    vm.runInContext(showPromptInfoSource, context);
    vm.runInContext('showPromptInfo()', context);

    const imageSection = findByClass(chatMessagesContainer, 'prompt-image-section');
    const avatar = imageSection.children.find(child => child.tagName === 'img');
    assert.ok(avatar);
    assert.equal(avatar.src, signed128);
    assert.equal(avatar.dataset.fullsize, signedFullsize);
    avatar.onclick();
    assert.deepEqual(openedUrls, [signedFullsize]);

    chatMessagesContainer.children = [];
    chatMessagesContainer.firstChild = null;
    context.botProfilePicture128 = '';
    context.botProfilePictureFullsize = '';
    vm.runInContext('showPromptInfo()', context);

    const fallbackSection = findByClass(chatMessagesContainer, 'prompt-image-section');
    const fallbackAvatar = fallbackSection.children.find(child => child.tagName === 'img');
    assert.equal(fallbackAvatar.src, signed32);
    assert.equal(fallbackAvatar.dataset.fullsize, signed32);

    const voiceSource = fs.readFileSync(
        path.join(repoRoot, 'data/static/js/chat/voice-call.js'),
        'utf8'
    );
    const voiceAvatarSource = extract(
        voiceSource,
        'const voiceAvatarUrl = (',
        'promptName.textContent = data.prompt_name;'
    );
    const voicePromptAvatar = new FakeElement('div');
    const voiceContext = {
        document: {
            createElement(tagName) { return new FakeElement(tagName); },
        },
        promptAvatar: voicePromptAvatar,
        data: { prompt_name: 'Assistant' },
        botProfilePicture: signed32,
        botProfilePicture128: signed128,
        botProfilePictureFullsize: signedFullsize,
    };
    vm.createContext(voiceContext);
    vm.runInContext(voiceAvatarSource, voiceContext);

    const voiceAvatar = voicePromptAvatar.children.find(child => child.tagName === 'img');
    assert.ok(voiceAvatar);
    assert.equal(voiceAvatar.src, signedFullsize);
    assert.doesNotMatch(voiceAvatarSource, /\.replace\(/);

    const avatarAssignmentSource = extract(
        source,
        'botProfilePicture = conversationInfo.bot_profile_picture',
        'const sidebarEl = document.querySelector'
    );
    const assignmentContext = {
        conversationInfo: {
            bot_profile_picture: signed32,
            bot_profile_picture_128: signed128,
            bot_profile_picture_fullsize: signedFullsize,
        },
        botProfilePicture: '',
        botProfilePicture128: '',
        botProfilePictureFullsize: '',
    };
    vm.createContext(assignmentContext);
    vm.runInContext(avatarAssignmentSource, assignmentContext);
    assert.equal(assignmentContext.botProfilePicture, signed32);
    assert.equal(assignmentContext.botProfilePicture128, signed128);
    assert.equal(assignmentContext.botProfilePictureFullsize, signedFullsize);

    assignmentContext.conversationInfo = {};
    vm.runInContext(avatarAssignmentSource, assignmentContext);
    assert.equal(assignmentContext.botProfilePicture, '');
    assert.equal(assignmentContext.botProfilePicture128, '');
    assert.equal(assignmentContext.botProfilePictureFullsize, '');
});

test('chat navigation and selectors guard stale asynchronous responses', () => {
    const source = fs.readFileSync(chatPath, 'utf8');

    assert.match(source, /let conversationViewGeneration = 0/);
    assert.match(source, /activeMessageLoad !== loadState/);
    assert.match(source, /!isCurrentConversationView\(conversationId, viewGeneration\)/);
    assert.match(source, /releaseActiveMessageLoad\(loadState, true\)/);
    assert.match(source, /signal: detailsController\.signal/);
    assert.match(source, /class ModelSelector[\s\S]*signal: state\.controller\.signal/);
    assert.match(source, /class ExtensionSelector[\s\S]*signal: state\.controller\.signal/);
    assert.match(source, /isCurrentConversationView\(state\.conversationId, state\.viewGeneration\)/);
    assert.match(source, /stopAudioAndCloseWebSocket\(\)/);
    assert.doesNotMatch(source, /stopAudioAndWebSocket\(\)/);
});

test('bookmarks has one click handler and response-local deduplication', () => {
    const source = fs.readFileSync(chatPath, 'utf8');

    assert.equal(
        (source.match(/myBookmarksButton\.addEventListener\('click'/g) || []).length,
        1
    );
    assert.equal(
        (source.match(/else if \(e\.target.*my-bookmarks-btn/g) || []).length,
        0
    );
    assert.match(source, /const localProcessedMessageIds = new Set\(\)/);
    assert.match(source, /const bookmarksController = new AbortController\(\)/);
    assert.match(source, /activeBookmarksLoad === loadState/);
});

test('removing sent attachment A preserves attachment B and its preview', () => {
    const source = fs.readFileSync(fileHandlingPath, 'utf8');
    const fileA = { name: 'a.pdf' };
    const fileB = { name: 'b.pdf' };
    const children = [];
    const makePreview = file => {
        const preview = {
            _aurvekAttachedFile: file,
            remove() {
                const index = children.indexOf(preview);
                if (index >= 0) children.splice(index, 1);
            },
        };
        children.push(preview);
        return preview;
    };
    makePreview(fileA);
    makePreview(fileB);
    const previews = {
        children,
        classList: { toggle() {} },
    };
    const fileInput = { value: 'selected' };
    const context = {
        window: {},
        attachedFiles: [fileA, fileB],
        document: {
            getElementById(id) {
                return id === 'image-previews' ? previews : fileInput;
            },
        },
        console,
        Array,
        Set,
        WeakMap,
        Promise,
        Object,
        Math,
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    context.window.removeAttachedFileBatch([fileA]);

    assert.deepEqual(context.attachedFiles, [fileB]);
    assert.equal(children.length, 1);
    assert.equal(children[0]._aurvekAttachedFile, fileB);

    const chatSource = fs.readFileSync(chatPath, 'utf8');
    assert.match(chatSource, /const outgoingFiles = Object\.freeze\(Array\.from\(/);
    assert.match(source, /const uploadBatch = Object\.freeze\(Array\.from\(files \|\| \[\]\)\)/);
    assert.doesNotMatch(chatSource, /attachedFiles\s*=\s*\[\]/);
});
