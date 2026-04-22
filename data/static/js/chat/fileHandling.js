const MAX_PDF_SIZE_MB = 25;
const MAX_IMAGE_SIZE_MB = 20;
const MAX_TEXT_SIZE_MB = 2;

const TEXT_FILE_EXTENSIONS = new Set([
    '.txt', '.md', '.csv', '.json', '.xml', '.html', '.htm',
    '.py', '.js', '.ts', '.css', '.sql', '.yaml', '.yml', '.toml',
    '.ini', '.cfg', '.conf', '.log', '.sh', '.bash',
    '.java', '.c', '.cpp', '.h', '.hpp', '.go', '.rs', '.rb',
    '.php', '.r', '.swift', '.kt', '.lua'
]);

const COMPRESSION_THRESHOLD_BYTES = 3 * 1024 * 1024; // 3MB - matches server SIZE_THRESHOLD
const COMPRESSION_QUALITY = 0.85;
const MAX_SHRINK_STEPS = 10;
const COMPRESSIBLE_TYPES = new Set([
    'image/png', 'image/bmp', 'image/tiff', 'image/gif',
    'image/jpeg', 'image/webp'
]);

// Tracks in-flight compression operations. Exported for chat.js send guard.
let compressionInProgress = 0;

function getCompressionTargetBytes() {
    const sizeMb = Number(Config.max_api_image_size_mb);
    if (Number.isFinite(sizeMb) && sizeMb > 0) {
        return sizeMb * 1024 * 1024;
    }
    // Safe fallback: matches the current backend default in common.py.
    return 5 * 1024 * 1024;
}

// ORDERING HAZARD (do NOT revert this to a module-level const):
// fileHandling.js is loaded by chat.html at line 714. The inline <script>
// block that assigns Config.max_chat_image_dimension runs at chat.html:737,
// AFTER this file has already been parsed and its top-level const declarations
// have been evaluated. A module-level
//     const COMPRESSION_MAX_DIMENSION = Config.max_chat_image_dimension || 1568;
// would always resolve to the fallback 1568 on initial page load, silently
// defeating the backend -> frontend Config channel. If the cap is ever tuned
// in common.py, a module-level const would NOT pick it up on reload.
// Read inside the function body instead, mirroring getCompressionTargetBytes()
// above. This is the F1 fix from round 8 external review.
function getCompressionMaxDimension() {
    const dim = Number(Config.max_chat_image_dimension);
    if (Number.isFinite(dim) && dim > 0) {
        return dim;
    }
    // Safe fallback: matches common.py's MAX_CHAT_IMAGE_DIMENSION.
    return 1568;
}

/**
 * Compress an image file to WebP if it exceeds the size threshold.
 * Uses Canvas API -- no external libraries.
 *
 * @param {File|Blob} file - The image file to potentially compress
 * @returns {Promise<File|Blob>} - Compressed file, or original if compression
 *   is not beneficial, not applicable, or fails
 */
async function maybeCompressImage(file) {
    // Only compress image types we know how to handle
    if (!file.type || !COMPRESSIBLE_TYPES.has(file.type)) return file;

    try {
        const compressionTargetBytes = getCompressionTargetBytes();
        const maxDim = getCompressionMaxDimension();
        const bitmap = await createImageBitmap(file);
        const exceedsDimensionCap = bitmap.width > maxDim || bitmap.height > maxDim;

        if (file.size <= COMPRESSION_THRESHOLD_BYTES && !exceedsDimensionCap) {
            bitmap.close();
            return file;
        }

        let width = bitmap.width;
        let height = bitmap.height;
        let compressedBlob = null;

        // Scale down first if any dimension exceeds the hard cap.
        if (exceedsDimensionCap) {
            const scale = maxDim / Math.max(width, height);
            width = Math.round(width * scale);
            height = Math.round(height * scale);
        }

        try {
            for (let step = 0; step < MAX_SHRINK_STEPS; step++) {
                const canvas = new OffscreenCanvas(width, height);
                const ctx = canvas.getContext('2d');
                if (!ctx) return file;

                ctx.drawImage(bitmap, 0, 0, width, height);
                compressedBlob = await canvas.convertToBlob({
                    type: 'image/webp',
                    quality: COMPRESSION_QUALITY
                });

                if (!compressedBlob || compressedBlob.size === 0) return file;
                if (compressedBlob.size <= compressionTargetBytes) break;
                if (width === 1 && height === 1) break;

                // Try again with a smaller frame if we are still above the backend gate.
                const scale = 0.85;
                width = Math.max(1, Math.round(width * scale));
                height = Math.max(1, Math.round(height * scale));
            }
        } finally {
            bitmap.close();
        }

        if (!compressedBlob || compressedBlob.size === 0) return file;

        // If we still could not fit under the backend gate, keep the original
        // so the existing server-side path can make the final decision.
        if (compressedBlob.size > compressionTargetBytes) return file;

        // Only use compressed version if it is actually smaller.
        if (compressedBlob.size >= file.size && !exceedsDimensionCap) return file;

        // Build a proper File with a corrected name (.webp extension)
        const originalName = file.name || 'image';
        const baseName = originalName.replace(/\.[^.]+$/, '');
        const compressedFile = new File(
            [compressedBlob],
            baseName + '.webp',
            { type: 'image/webp', lastModified: Date.now() }
        );

        const originalMB = (file.size / (1024 * 1024)).toFixed(1);
        const compressedMB = (compressedFile.size / (1024 * 1024)).toFixed(1);
        console.log(`[compression] ${originalName}: ${originalMB}MB -> ${compressedMB}MB`);

        return compressedFile;
    } catch (err) {
        // Fallback: send original file if Canvas compression fails
        console.warn('[compression] Failed, sending original:', err);
        return file;
    }
}

function isTextFile(file) {
    if (!file.name) return false;
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    if (!TEXT_FILE_EXTENSIONS.has(ext)) return false;
    const MIME_EXCEPTIONS = new Set(['video/mp2t']);
    if (file.type && !MIME_EXCEPTIONS.has(file.type)
        && (file.type.startsWith('image/') || file.type === 'application/pdf'
        || file.type.startsWith('audio/') || file.type.startsWith('video/'))) return false;
    return true;
}

function isAcceptedFileType(file) {
    return file.type.startsWith('image/') || file.type === 'application/pdf' || isTextFile(file);
}

function validateFileSize(file) {
    const sizeMB = file.size / (1024 * 1024);
    if (file.type === 'application/pdf' && sizeMB > MAX_PDF_SIZE_MB) {
        NotificationModal.warning('File too large', `PDF files must be under ${MAX_PDF_SIZE_MB}MB`);
        return false;
    } else if (isTextFile(file)) {
        if (sizeMB > MAX_TEXT_SIZE_MB) {
            NotificationModal.warning('File too large', `Text files must be under ${MAX_TEXT_SIZE_MB}MB`);
            return false;
        }
    } else if (file.type.startsWith('image/') && sizeMB > MAX_IMAGE_SIZE_MB) {
        NotificationModal.warning('File too large', `Images must be under ${MAX_IMAGE_SIZE_MB}MB`);
        return false;
    }
    return true;
}

async function handlePasteEvent(event) {

    if (!Config.can_send_files) return;
    const items = (event.clipboardData || event.originalEvent.clipboardData).items;

    // Collect image blobs first (clipboard items are ephemeral -- extract immediately)
    const imageBlobs = [];
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === 'file' && item.type.startsWith('image/')) {
            const blob = item.getAsFile();
            if (!blob || !validateFileSize(blob)) continue;
            imageBlobs.push(blob);
        }
    }

    if (imageBlobs.length === 0) return;

    compressionInProgress++;
    try {
        let compressedCount = 0;
        let totalSavedBytes = 0;

        for (const blob of imageBlobs) {
            const processed = await maybeCompressImage(blob);

            attachedFiles.push(processed);
            const reader = new FileReader();
            reader.onload = function(event){
                const img = document.createElement('img');
                img.src = event.target.result;
                img.className = 'preview-image';
                document.getElementById('image-previews').appendChild(img);
                document.getElementById('image-previews').classList.remove('hidden');
            };
            reader.readAsDataURL(processed);

            if (processed !== blob && processed.size < blob.size) {
                compressedCount++;
                totalSavedBytes += blob.size - processed.size;
            }
        }

        if (compressedCount > 0) {
            const saved = (totalSavedBytes / (1024 * 1024)).toFixed(1);
            const msg = compressedCount === 1
                ? `Image compressed (saved ${saved} MB)`
                : `${compressedCount} images compressed (saved ${saved} MB)`;
            NotificationModal.toast(msg, 'info', 3000);
        }
    } finally {
        compressionInProgress--;
    }

    setTimeout(() => {
        document.getElementById('message-text').focus();
    }, 0);
}

function processFiles(files, formData, imagePreviews) {
    for (var i = 0; i < files.length; i++) {
        if (!isAcceptedFileType(files[i]) || !validateFileSize(files[i])) {
            if (!isAcceptedFileType(files[i])) {
                NotificationModal.warning('Invalid File', 'Only image, PDF, and text files are allowed.');
            }
            continue;
        }

        formData.append('file', files[i]);

        if (files[i].type === 'application/pdf') {
            // Show PDF attachment in chat (mirrors the image pattern)
            var userMessageElement = document.createElement('div');
            userMessageElement.classList.add('message', 'user');
            var pdfAttachment = document.createElement('div');
            pdfAttachment.className = 'chat-pdf-attachment';
            var badge = document.createElement('span');
            badge.className = 'pdf-badge';
            badge.textContent = 'PDF';
            var label = document.createElement('span');
            label.textContent = ' ' + files[i].name;  // textContent — XSS safe
            pdfAttachment.appendChild(badge);
            pdfAttachment.appendChild(label);
            userMessageElement.appendChild(pdfAttachment);

            var chatMessagesContainer = document.getElementById('chat-messages-container');
            chatMessagesContainer.appendChild(userMessageElement);
            var chatWindow = document.getElementById('chat-window');
            chatWindow.scrollTop = chatWindow.scrollHeight;

            imagePreviews.innerHTML = '';
            imagePreviews.classList.add('hidden');
            document.getElementById('chat-files').value = '';
        } else if (isTextFile(files[i])) {
            imagePreviews.innerHTML = '';
            imagePreviews.classList.add('hidden');
            document.getElementById('chat-files').value = '';

            var msgDiv = document.createElement('div');
            msgDiv.className = 'message user';
            var textAttachment = document.createElement('div');
            textAttachment.className = 'chat-text-attachment';
            var badge = document.createElement('span');
            badge.className = 'text-badge';
            badge.textContent = 'TXT';
            var label = document.createElement('span');
            label.textContent = files[i].name;
            textAttachment.appendChild(badge);
            textAttachment.appendChild(label);
            msgDiv.appendChild(textAttachment);
            var chatMessagesContainer = document.getElementById('chat-messages-container');
            chatMessagesContainer.appendChild(msgDiv);
            var chatWindow = document.getElementById('chat-window');
            chatWindow.scrollTop = chatWindow.scrollHeight;
        } else {
            // Existing image preview logic
            var reader = new FileReader();
            reader.onload = function (e) {
                var img = document.createElement('img');
                img.src = e.target.result;
                img.className = 'preview-image';
                imagePreviews.appendChild(img);

                var userMessageElement = document.createElement('div');
                userMessageElement.classList.add('message', 'user');

                var userMessageImage = document.createElement('img');
                userMessageImage.src = e.target.result;
                userMessageImage.style.maxWidth = '100%';
                userMessageImage.style.height = 'auto';
                userMessageImage.style.display = 'block';
                userMessageImage.style.margin = '0 auto';

                userMessageElement.appendChild(userMessageImage);

                var chatMessagesContainer = document.getElementById('chat-messages-container');
                chatMessagesContainer.appendChild(userMessageElement);

                var chatWindow = document.getElementById('chat-window');
                chatWindow.scrollTop = chatWindow.scrollHeight;

                imagePreviews.innerHTML = '';
                imagePreviews.classList.add('hidden');
                document.getElementById('chat-files').value = '';
            };
            reader.onerror = function (e) {
                console.error('Error reading file:', e);
            };
            reader.readAsDataURL(files[i]);
        }
    }
    if (imagePreviews.children.length > 0) {
        imagePreviews.classList.remove('hidden');
    }
}


function initFileHandling() {
    if (!Config.can_send_files) return;
    document.addEventListener('paste', handlePasteEvent);
    const fileInput = document.getElementById('chat-files');
    if (fileInput) fileInput.addEventListener('change', handleFileSelect);
}

async function handleFileSelect(event) {
    const files = event.target.files;
    const imagePreviews = document.getElementById('image-previews');

    compressionInProgress++;
    try {
        let compressedCount = 0;
        let totalSavedBytes = 0;

        for (const file of files) {
            if (!isAcceptedFileType(file) || !validateFileSize(file)) {
                if (!isAcceptedFileType(file)) {
                    NotificationModal.warning('Invalid File', 'Only image, PDF, and text files are allowed.');
                }
                continue;
            }

            if (file.type === 'application/pdf') {
                // PDF preview (unchanged)
                const pdfPreview = document.createElement('div');
                pdfPreview.className = 'pdf-preview-item';
                const iconSpan = document.createElement('span');
                iconSpan.className = 'pdf-icon';
                iconSpan.textContent = 'PDF';
                const nameSpan = document.createElement('span');
                nameSpan.className = 'pdf-name';
                nameSpan.textContent = file.name;
                pdfPreview.appendChild(iconSpan);
                pdfPreview.appendChild(nameSpan);
                imagePreviews.appendChild(pdfPreview);
                attachedFiles.push(file);
            } else if (isTextFile(file)) {
                // Text preview (unchanged)
                const previewItem = document.createElement('div');
                previewItem.className = 'text-preview-item';
                const icon = document.createElement('span');
                icon.className = 'text-icon';
                icon.textContent = 'TXT';
                const name = document.createElement('span');
                name.className = 'text-name';
                name.textContent = file.name;
                previewItem.appendChild(icon);
                previewItem.appendChild(name);
                imagePreviews.appendChild(previewItem);
                attachedFiles.push(file);
            } else {
                // Image: compress before preview
                const processed = await maybeCompressImage(file);
                attachedFiles.push(processed);

                const reader = new FileReader();
                reader.onload = function (e) {
                    const img = document.createElement('img');
                    img.src = e.target.result;
                    img.className = 'preview-image';
                    imagePreviews.appendChild(img);
                };
                reader.onerror = function (e) {
                    console.error('Error reading file:', e);
                };
                reader.readAsDataURL(processed);

                if (processed !== file && processed.size < file.size) {
                    compressedCount++;
                    totalSavedBytes += file.size - processed.size;
                }
            }
        }

        if (compressedCount > 0) {
            const saved = (totalSavedBytes / (1024 * 1024)).toFixed(1);
            const msg = compressedCount === 1
                ? `Image compressed (saved ${saved} MB)`
                : `${compressedCount} images compressed (saved ${saved} MB)`;
            NotificationModal.toast(msg, 'info', 3000);
        }
    } finally {
        compressionInProgress--;
    }

    if (attachedFiles.length > 0) {
        imagePreviews.classList.remove('hidden');
    }
    document.getElementById('message-text').focus();
}
