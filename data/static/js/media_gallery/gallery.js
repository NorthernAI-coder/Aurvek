let currentImageIndex = 0;
const imagesPerPage = 20;
let currentPage = 1;
let currentTab = 'all';

// Image rendering functions
function renderImages(imageSet, containerId) {
    const container = document.getElementById(containerId);
    container.innerHTML = '';
    const startIndex = (currentPage - 1) * imagesPerPage;
    const endIndex = startIndex + imagesPerPage;
    const imagesToRender = imageSet.slice(startIndex, endIndex);

    const fragment = document.createDocumentFragment();
    imagesToRender.forEach((image, index) => {
        const fullsizeUrl = image.fullsize_url || image.url;
        const div = document.createElement('div');
        div.className = 'image-container';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'image-checkbox';
        checkbox.dataset.id = String(image.id);
        checkbox.dataset.attachmentRef = image.attachment_ref || '';
        div.appendChild(checkbox);

        const img = document.createElement('img');
        img.src = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
        img.dataset.src = image.url || '';
        img.alt = `${image.type || ''} image`;
        img.className = 'gallery-image lazy';
        img.addEventListener('click', () => {
            FullsizeViewer.show(fullsizeUrl, startIndex + index);
        });
        div.appendChild(img);

        fragment.appendChild(div);
    });
    container.appendChild(fragment);

    lazyLoadImages();
}

// Lazy loading of images
function lazyLoadImages() {
    const lazyImages = document.querySelectorAll('img.lazy');
    const imageObserver = new IntersectionObserver((entries, observer) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                const img = entry.target;
                img.src = img.dataset.src;
                img.classList.remove('lazy');
                imageObserver.unobserve(img);
            }
        });
    });

    lazyImages.forEach(img => imageObserver.observe(img));
}

// Modify the renderPagination function
function renderPagination(totalImages, tabId) {
    const totalPages = Math.ceil(totalImages / imagesPerPage);
    const pagination = document.getElementById(`pagination-${tabId}`);
    if (!pagination) return;

    pagination.innerHTML = '';

    for (let i = 1; i <= totalPages; i++) {
        const li = document.createElement('li');
        li.className = `page-item ${i === currentPage ? 'active' : ''}`;
        li.innerHTML = `<a class="page-link" href="#" onclick="changePage(${i}, '${tabId}')">${i}</a>`;
        pagination.appendChild(li);
    }
}

// Modify the changePage function
function changePage(page, tabId) {
    currentPage = page;
    renderImagesForCurrentTab(tabId);
}

// Modify the renderImagesForCurrentTab function
function renderImagesForCurrentTab(tabId = currentTab) {
    const containerId = `${tabId}Images`;
    const container = document.getElementById(containerId);
    if (!container) {
        return;
    }
    let filteredImages;
    switch (tabId) {
        case 'bot':
            filteredImages = images.filter(img => img.type === 'bot');
            break;
        case 'user':
            filteredImages = images.filter(img => img.type === 'user');
            break;
        default:
            filteredImages = images;
    }
    renderImages(filteredImages, `${tabId}Images`);
    renderPagination(filteredImages.length, tabId);

    // Update FullsizeViewer with current images
    FullsizeViewer.setImages(filteredImages.map(img => ({
        ...img,
        url: img.fullsize_url || img.url
    })));
}

// Image deletion functions
function deleteImageAttachments(attachmentRefs) {
    if (!attachmentRefs.length) {
        return Promise.resolve();
    }
    return Promise.all(attachmentRefs.map(ref =>
        fetch(`/api/attachments/${encodeURIComponent(ref)}`, { method: 'DELETE' })
    )).then(responses => {
        const failed = responses.filter(response => !response.ok).length;
        if (failed) {
            throw new Error(`${failed} image(s) could not be deleted`);
        }
    });
}

function deleteImages(imageIds, attachmentRefs = []) {
    const legacyIds = imageIds || [];
    const attachmentDeletes = deleteImageAttachments(attachmentRefs);
    const legacyDelete = legacyIds.length ? fetch('/delete-images', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(legacyIds),
    }).then(response => response.json()) : Promise.resolve({ message: '' });

    Promise.all([attachmentDeletes, legacyDelete])
    .then(([, data]) => {
        NotificationModal.info('Images Deleted', data.message || 'Images deleted');
        location.reload();
    })
    .catch((error) => {
        console.error('Error:', error);
        NotificationModal.error('Delete Error', 'An error occurred while deleting the images.');
    });
}

// Delete single image from viewer
function deleteCurrentImage(url, index, imageData) {
    const attachmentRef = imageData?.attachment_ref || null;
    const imageId = imageData?.id;
    if (!attachmentRef && (imageId === undefined || imageId === null)) {
        NotificationModal.error('Delete Error', 'The displayed image could not be identified.');
        return;
    }

    NotificationModal.confirm('Delete Image', 'Are you sure you want to delete this image?', () => {
        // Use the immutable identity captured when this image finished loading.
        // Navigation while the confirmation modal is open cannot change it.
        if (attachmentRef) {
            deleteImages([], [attachmentRef]);
        } else {
            deleteImages([imageId]);
        }
    }, null, { type: 'error', confirmText: 'Delete' });
}

function imageDeletePayload(selectedImages) {
    const idsToDelete = [];
    const refsToDelete = [];
    Array.from(selectedImages).forEach(checkbox => {
        if (checkbox.dataset.attachmentRef) {
            refsToDelete.push(checkbox.dataset.attachmentRef);
        } else {
            idsToDelete.push(parseInt(checkbox.dataset.id));
        }
    });
    return { idsToDelete, refsToDelete };
}

// Event Listeners
document.addEventListener('DOMContentLoaded', function() {
    // Initialize FullsizeViewer with navigation and delete
    FullsizeViewer.init({
        showNav: true,
        showDownload: true,
        showDelete: true,
        onDelete: deleteCurrentImage,
        images: images
    });

    // Multiple deletion buttons for each tab
    document.getElementById('deleteSelectedAll')?.addEventListener('click', function() {
        const selectedImages = document.querySelectorAll('#all .image-checkbox:checked');
        if (selectedImages.length > 0) {
            NotificationModal.confirm('Delete Images', `Are you sure you want to delete ${selectedImages.length} selected images?`, () => {
                const { idsToDelete, refsToDelete } = imageDeletePayload(selectedImages);
                deleteImages(idsToDelete, refsToDelete);
            }, null, { type: 'error', confirmText: 'Delete' });
        } else {
            NotificationModal.warning('Selection Required', 'Please select at least one image to delete');
        }
    });

    document.getElementById('deleteSelectedBot')?.addEventListener('click', function() {
        const selectedImages = document.querySelectorAll('#bot .image-checkbox:checked');
        if (selectedImages.length > 0) {
            NotificationModal.confirm('Delete Images', `Are you sure you want to delete ${selectedImages.length} selected images?`, () => {
                const { idsToDelete, refsToDelete } = imageDeletePayload(selectedImages);
                deleteImages(idsToDelete, refsToDelete);
            }, null, { type: 'error', confirmText: 'Delete' });
        } else {
            NotificationModal.warning('Selection Required', 'Please select at least one image to delete');
        }
    });

    document.getElementById('deleteSelectedUser')?.addEventListener('click', function() {
        const selectedImages = document.querySelectorAll('#user .image-checkbox:checked');
        if (selectedImages.length > 0) {
            NotificationModal.confirm('Delete Images', `Are you sure you want to delete ${selectedImages.length} selected images?`, () => {
                const { idsToDelete, refsToDelete } = imageDeletePayload(selectedImages);
                deleteImages(idsToDelete, refsToDelete);
            }, null, { type: 'error', confirmText: 'Delete' });
        } else {
            NotificationModal.warning('Selection Required', 'Please select at least one image to delete');
        }
    });

    // Tab switching (modify existing to include clearing selections)
    document.querySelectorAll('button[data-bs-toggle="tab"]').forEach(tab => {
        tab.addEventListener('shown.bs.tab', function (event) {
            currentTab = event.target.id.replace('-tab', '');
            currentPage = 1;
            renderImagesForCurrentTab(currentTab);

            // Clear selections when changing tabs
            document.querySelectorAll('.image-checkbox:checked').forEach(checkbox => {
                checkbox.checked = false;
            });
        });
    });

    // Initial rendering
    renderImagesForCurrentTab();
});
