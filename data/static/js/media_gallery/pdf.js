/////////////  V2 - Simplified (no iframe modal) \\\\\\\\\\\\

// Global variables for PDFs
let allPdfs = [];
let currentPdfPage = 1;

/**
 * Opens a PDF in a new browser tab
 * @param {string} path - Path of the PDF file
 */
function openPdf(path) {
    const baseUrl = path.startsWith('/api/attachments/')
        ? window.location.origin
        : (window.cdnFilesUrl || window.location.origin);
    const url = new URL(path, baseUrl);
    if (window.pdfToken) {
        url.searchParams.append('token', window.pdfToken);
    }
    window.open(url.toString(), '_blank');
}

/**
 * Downloads a PDF file
 * @param {string} path - Path of the PDF file
 */
function downloadPdf(path) {
    const baseUrl = path.startsWith('/api/attachments/')
        ? window.location.origin
        : (window.cdnFilesUrl || window.location.origin);
    const url = new URL(path, baseUrl);
    if (window.pdfToken) {
        url.searchParams.append('token', window.pdfToken);
        url.searchParams.append('download', 'true');
    }
    window.open(url.toString(), '_blank');
}

/**
 * Deletes a single PDF from the server
 * @param {string} encodedPath - URL-encoded path of the PDF to delete
 */
function deletePdf(encodedPath, attachmentRef = '') {
    const path = decodeURIComponent(encodedPath);

    NotificationModal.confirm('Delete PDF', 'Are you sure you want to delete this PDF?', () => {
        if (attachmentRef) {
            fetch(`/api/attachments/${encodeURIComponent(attachmentRef)}`, {
                method: 'DELETE'
            })
            .then(async response => {
                if (!response.ok) {
                    const errorText = await response.text();
                    throw new Error(`HTTP error! status: ${response.status}, message: ${errorText}`);
                }
                return response.json();
            })
            .then(data => {
                NotificationModal.info('PDF Deleted', data.message || 'PDF deleted');
                allPdfs = allPdfs.filter(pdf => pdf.attachment_ref !== attachmentRef);
                renderPdfPage(currentPdfPage);
            })
            .catch((error) => {
                console.error('Error deleting PDF:', error);
                NotificationModal.error('Delete Error', `Error deleting PDF: ${error.message}`);
            });
            return;
        }

        const payload = {
            pdf_path: path.startsWith('data/') ? path : `data${path}`
        };

        fetch('/delete-pdf', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(payload)
        })
        .then(async response => {
            if (!response.ok) {
                const errorText = await response.text();
                throw new Error(`HTTP error! status: ${response.status}, message: ${errorText}`);
            }
            return response.json();
        })
        .then(data => {
            NotificationModal.info('PDF Deleted', data.message);

            // Remove PDF from array and re-render
            allPdfs = allPdfs.filter(pdf => pdf.path !== path && pdf.nginx_path !== path);

            const totalPages = Math.ceil(allPdfs.length / ITEMS_PER_PAGE);
            if (currentPdfPage > totalPages && totalPages > 0) {
                currentPdfPage = totalPages;
            }
            renderPdfPage(currentPdfPage);
        })
        .catch((error) => {
            console.error('Error deleting PDF:', error);
            NotificationModal.error('Delete Error', `Error deleting PDF: ${error.message}`);
        });
    }, null, { type: 'error', confirmText: 'Delete' });
}

/**
 * Deletes multiple selected PDFs
 */
function deleteSelectedPdfs() {
    const selectedPdfs = document.querySelectorAll('.pdf-checkbox:checked');
    if (selectedPdfs.length === 0) {
        NotificationModal.warning('Selection Required', 'Please select at least one PDF to delete');
        return;
    }

    NotificationModal.confirm('Delete PDFs', `Are you sure you want to delete ${selectedPdfs.length} selected PDFs?`, () => {
        const attachmentRefs = Array.from(selectedPdfs)
            .map(checkbox => checkbox.dataset.attachmentRef)
            .filter(Boolean);
        const pdfPaths = Array.from(selectedPdfs)
            .filter(checkbox => !checkbox.dataset.attachmentRef)
            .map(checkbox => decodeURIComponent(checkbox.dataset.path));

        if (attachmentRefs.length > 0 && pdfPaths.length === 0) {
            Promise.all(attachmentRefs.map(ref =>
                fetch(`/api/attachments/${encodeURIComponent(ref)}`, { method: 'DELETE' })
            ))
            .then(async responses => {
                const failed = responses.filter(response => !response.ok).length;
                if (failed) {
                    throw new Error(`${failed} PDF(s) could not be deleted`);
                }
                NotificationModal.info('PDFs Deleted', `Successfully deleted: ${attachmentRefs.length}, Failed: 0`);
                location.reload();
            })
            .catch(error => {
                console.error('Error deleting PDFs:', error);
                NotificationModal.error('Delete Error', error.message);
            });
            return;
        }

        fetch('/delete-pdfs', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ pdf_paths: pdfPaths, attachment_refs: attachmentRefs })
        })
        .then(async response => {
            if (!response.ok) {
                const errorText = await response.text();
                throw new Error(`HTTP error! status: ${response.status}, message: ${errorText}`);
            }
            return response.json();
        })
        .then(data => {
            NotificationModal.info('PDFs Deleted', data.message);
            location.reload();
        })
        .catch(error => {
            console.error('Error deleting PDFs:', error);
            NotificationModal.error('Delete Error', 'An error occurred while deleting the PDFs');
        });
    }, null, { type: 'error', confirmText: 'Delete' });
}

/**
 * Loads PDFs from the server
 */
function loadPDFs() {
    fetch('/get-pdfs')
        .then(response => response.json())
        .then(data => {
            allPdfs = data.pdfs;
            window.pdfToken = data.pdf_token;
            renderPdfPage(1);
        })
        .catch(error => {
            console.error('Error loading PDFs:', error);
            NotificationModal.error('Load Error', 'Error loading PDFs');
        });
}

/**
 * Renders a page of PDFs
 * @param {number} page - Page number to render
 */
function renderPdfPage(page) {
    currentPdfPage = page;
    const pageData = paginationUtils.getCurrentPageData(allPdfs, page, ITEMS_PER_PAGE);
    const container = document.getElementById('pdfContainer');

    container.innerHTML = '';
    const fragment = document.createDocumentFragment();

    pageData.forEach(pdf => {
        const pdfContainer = document.createElement('div');
        pdfContainer.className = 'pdf-container';

        const wrapper = document.createElement('div');
        wrapper.className = 'pdf-wrapper';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'pdf-checkbox';
        checkbox.dataset.path = encodeURIComponent(pdf.path || '');
        checkbox.dataset.attachmentRef = pdf.attachment_ref || '';
        checkbox.title = 'Select for bulk delete';
        wrapper.appendChild(checkbox);

        const icon = document.createElement('div');
        icon.className = 'pdf-icon';
        icon.title = 'Click to open PDF';
        icon.addEventListener('click', () => openPdf(pdf.nginx_path));
        const iconInner = document.createElement('i');
        iconInner.className = 'fas fa-file-pdf';
        icon.appendChild(iconInner);
        wrapper.appendChild(icon);

        const info = document.createElement('div');
        info.className = 'pdf-info';
        const name = document.createElement('div');
        name.className = 'pdf-name';
        name.title = pdf.name || '';
        name.textContent = pdf.name || '';
        info.appendChild(name);

        const controls = document.createElement('div');
        controls.className = 'pdf-controls';

        const downloadButton = document.createElement('button');
        downloadButton.className = 'btn btn-sm btn-primary';
        downloadButton.title = 'Download PDF';
        downloadButton.innerHTML = '<i class="fas fa-download"></i> Download';
        downloadButton.addEventListener('click', () => downloadPdf(pdf.nginx_path));
        controls.appendChild(downloadButton);

        const deleteButton = document.createElement('button');
        deleteButton.className = 'btn btn-sm btn-danger';
        deleteButton.title = 'Delete PDF';
        deleteButton.innerHTML = '<i class="fas fa-trash"></i> Delete';
        deleteButton.addEventListener('click', () => {
            deletePdf(encodeURIComponent(pdf.path || ''), pdf.attachment_ref || '');
        });
        controls.appendChild(deleteButton);

        pdfContainer.appendChild(wrapper);
        pdfContainer.appendChild(info);
        pdfContainer.appendChild(controls);
        fragment.appendChild(pdfContainer);
    });

    container.appendChild(fragment);

    // Update pagination controls
    const paginationElement = document.getElementById('pagination-pdfs');
    paginationElement.innerHTML = paginationUtils.createPaginationControls(
        allPdfs.length,
        page,
        ITEMS_PER_PAGE,
        renderPdfPage
    );

    // Add event listeners to pagination controls
    paginationElement.querySelectorAll('.page-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const newPage = parseInt(e.target.dataset.page);
            if (!isNaN(newPage) && newPage > 0 && newPage <= Math.ceil(allPdfs.length / ITEMS_PER_PAGE)) {
                renderPdfPage(newPage);
            }
        });
    });
}

// Utility functions for PDF handling
const pdfUtils = {
    validatePath: function(path) {
        return path && typeof path === 'string' && path.toLowerCase().endsWith('.pdf');
    },
    getFileName: function(path) {
        return path.split('/').pop().split('\\').pop();
    }
};

// Event Listeners
document.addEventListener('DOMContentLoaded', function() {
    const pdfTab = document.getElementById('pdf-tab');
    pdfTab.addEventListener('shown.bs.tab', function (e) {
        if (!pdfTab.dataset.loaded) {
            loadPDFs();
            pdfTab.dataset.loaded = true;
        }
    });

    const deleteSelectedPdfsButton = document.getElementById('deleteSelectedPdfs');
    if (deleteSelectedPdfsButton) {
        deleteSelectedPdfsButton.addEventListener('click', deleteSelectedPdfs);
    }
});

// Export for modules
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        openPdf,
        downloadPdf,
        deletePdf,
        pdfUtils
    };
}
