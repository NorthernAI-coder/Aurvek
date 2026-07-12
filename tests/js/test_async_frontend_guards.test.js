const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..', '..');

function eventTarget(base = {}) {
    const listeners = new Map();
    return Object.assign(base, {
        addEventListener(type, callback) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(callback);
        },
        removeEventListener(type, callback) {
            const callbacks = listeners.get(type) || [];
            listeners.set(type, callbacks.filter(item => item !== callback));
        },
        dispatch(type, event = {}) {
            for (const callback of listeners.get(type) || []) callback(event);
        },
    });
}

function makeClassList() {
    const classes = new Set();
    return {
        add(...names) { names.forEach(name => classes.add(name)); },
        remove(...names) { names.forEach(name => classes.delete(name)); },
        contains(name) { return classes.has(name); },
    };
}

test('FormGuard keeps AJAX failures dirty regardless of listener order', async () => {
    const input = {
        name: 'title',
        value: 'before',
        type: 'text',
        disabled: false,
        tagName: 'INPUT',
    };
    const form = eventTarget({
        querySelectorAll() { return [input]; },
    });
    const document = eventTarget({
        querySelector() { return form; },
        querySelectorAll() { return []; },
    });
    const window = eventTarget({ location: { href: '', reload() {} } });
    const context = {
        window,
        document,
        CSS: { escape: value => value },
        NotificationModal: { confirm() {} },
        requestAnimationFrame: callback => callback(),
        queueMicrotask,
        Promise,
        Set,
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(repoRoot, 'data/static/js/common/form-guard.js'),
            'utf8'
        ),
        context
    );

    window.FormGuard.watch(form);
    input.value = 'after';

    // Register after FormGuard to exercise the ordering that used to leave
    // _fgSubmitting stuck at true.
    form.addEventListener('submit', event => event.preventDefault());
    const ajaxEvent = {
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
    };
    form.dispatch('submit', ajaxEvent);
    await Promise.resolve();

    assert.equal(window.FormGuard.anyDirty(), true);

    window.FormGuard.markClean(form);
    const nativeEvent = {
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
    };
    // Remove the AJAX listener by using a second watched form.
    const nativeInput = { ...input, value: 'before-native' };
    const nativeForm = eventTarget({ querySelectorAll() { return [nativeInput]; } });
    window.FormGuard.watch(nativeForm);
    nativeInput.value = 'after-native';
    nativeForm.dispatch('submit', nativeEvent);
    await Promise.resolve();
    assert.equal(window.FormGuard.isDirty(nativeForm), true);
    assert.equal(window.FormGuard.anyDirty(), false);

    window.FormGuard.resumeAfterSubmit(nativeForm);
    assert.equal(window.FormGuard.anyDirty(), true);
});

test('FullsizeViewer ignores stale image loads and deletes the displayed resource', () => {
    const elements = {
        '.fullsize-viewer-backdrop': eventTarget({ style: {} }),
        '.fullsize-viewer-close': eventTarget({ style: {} }),
        '.fullsize-viewer-prev': eventTarget({ style: {} }),
        '.fullsize-viewer-next': eventTarget({ style: {} }),
        '.fullsize-viewer-download': eventTarget({ style: {} }),
        '.fullsize-viewer-delete': eventTarget({ style: {}, disabled: false }),
        '.fullsize-viewer-controls': eventTarget({ style: {} }),
        '.fullsize-viewer-spinner': eventTarget({ style: {} }),
        '.fullsize-viewer-image': eventTarget({ style: {}, src: '' }),
    };
    const container = eventTarget({
        classList: makeClassList(),
        querySelector(selector) { return elements[selector]; },
    });

    let injected = false;
    const body = {
        insertAdjacentHTML() { injected = true; },
        appendChild() {},
        removeChild() {},
    };
    const document = eventTarget({
        body,
        getElementById(id) {
            return id === 'fullsizeViewer' && injected ? container : null;
        },
        createElement() {
            return { click() {}, href: '', download: '' };
        },
    });

    const imageInstances = [];
    class FakeImage {
        constructor() {
            this.onload = null;
            this.onerror = null;
            this._src = '';
            this.currentSrc = '';
            imageInstances.push(this);
        }
        set src(value) {
            this._src = value;
            this.currentSrc = value;
        }
        get src() { return this._src; }
        load() { if (this.onload) this.onload.call(this); }
    }

    const window = {};
    const context = { window, document, Image: FakeImage, Date, Object };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(repoRoot, 'data/static/js/fullsize-viewer.js'),
            'utf8'
        ),
        context
    );

    const deleted = [];
    const images = [
        { id: 'first', url: '/first.webp' },
        { id: 'second', url: '/second.webp' },
    ];
    window.FullsizeViewer.init({
        showNav: true,
        showDelete: true,
        transformUrl: false,
        images,
        onDelete(url, index, imageData) {
            deleted.push({ url, index, id: imageData.id });
        },
    });

    window.FullsizeViewer.show('/first.webp', 0);
    const staleLoader = imageInstances.at(-1);
    window.FullsizeViewer.next();
    const currentLoader = imageInstances.at(-1);
    currentLoader.load();
    staleLoader.load();

    assert.equal(window.FullsizeViewer.getDisplayedResource().imageData.id, 'second');
    elements['.fullsize-viewer-delete'].dispatch('click');
    assert.deepEqual(deleted, [{ url: '/second.webp', index: 1, id: 'second' }]);
});

test('audio recorder and folder chat code keep single-operation invariants', () => {
    const audioSource = fs.readFileSync(
        path.join(repoRoot, 'data/static/js/chat/audio.js'),
        'utf8'
    );
    const foldersSource = fs.readFileSync(
        path.join(repoRoot, 'data/static/js/chat/folders.js'),
        'utf8'
    );

    assert.equal(
        (audioSource.match(/mediaDevices\.getUserMedia\(\{ audio: true \}\)/g) || []).length,
        1
    );
    assert.equal(
        (audioSource.match(/addEventListener\('click', toggleAudioRecording\)/g) || []).length,
        1
    );
    assert.match(audioSource, /new AbortController\(\)/);
    assert.match(audioSource, /URL\.revokeObjectURL\(url\)/);
    assert.match(audioSource, /session\.recorder\.onstop = \(\) => handleAudioStop\(session\)/);

    assert.match(foldersSource, /const targetFolderId = incognito \? null : currentSelectedFolderId/);
    assert.match(foldersSource, /await loadFolderChats\(targetFolderId/);
    assert.match(foldersSource, /await updateFolderConversationCount\(targetFolderId\)/);
    assert.equal(
        (foldersSource.match(/originalStartNewConversation\(promptId, options\)/g) || []).length,
        1
    );
});
