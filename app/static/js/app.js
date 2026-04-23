document.addEventListener('DOMContentLoaded', () => {

    // ─── Paste Zone: Ctrl+V, drag-drop, click ───
    document.querySelectorAll('.paste-zone').forEach(zone => {
        const input = zone.querySelector('.paste-zone-input');
        const placeholder = zone.querySelector('.paste-zone-placeholder');
        const preview = zone.querySelector('.paste-zone-preview');
        const previewImg = preview.querySelector('img');
        const removeBtn = zone.querySelector('.paste-zone-remove');

        function showPreview(file) {
            if (!file || !file.type.startsWith('image/')) return;
            const reader = new FileReader();
            reader.onload = (e) => {
                previewImg.src = e.target.result;
                placeholder.style.display = 'none';
                preview.style.display = 'block';
                zone.classList.add('has-file');
            };
            reader.readAsDataURL(file);
        }

        function setFileToInput(file) {
            const dt = new DataTransfer();
            dt.items.add(file);
            input.files = dt.files;
            showPreview(file);
        }

        function clearFile() {
            input.value = '';
            previewImg.src = '';
            placeholder.style.display = 'flex';
            preview.style.display = 'none';
            zone.classList.remove('has-file');
        }

        // Click to select file
        input.addEventListener('change', () => {
            if (input.files[0]) showPreview(input.files[0]);
        });

        // Remove button
        removeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            e.preventDefault();
            clearFile();
        });

        // Ctrl+V paste — hover vào zone là nhận paste, không cần click
        let hovered = false;

        zone.addEventListener('mouseenter', () => {
            hovered = true;
        });
        zone.addEventListener('mouseleave', () => {
            hovered = false;
        });

        document.addEventListener('paste', (e) => {
            if (!hovered) return;
            const items = e.clipboardData?.items;
            if (!items) return;
            for (const item of items) {
                if (item.type.startsWith('image/')) {
                    e.preventDefault();
                    const file = item.getAsFile();
                    if (file) setFileToInput(file);
                    return;
                }
            }
        });

        // Drag & drop
        zone.addEventListener('dragover', (e) => {
            e.preventDefault();
            zone.classList.add('drag-over');
        });
        zone.addEventListener('dragleave', () => {
            zone.classList.remove('drag-over');
        });
        zone.addEventListener('drop', (e) => {
            e.preventDefault();
            zone.classList.remove('drag-over');
            const file = e.dataTransfer?.files[0];
            if (file && file.type.startsWith('image/')) {
                setFileToInput(file);
            }
        });
    });

    // ─── Settings inputs: select all on focus for easy replacement ───
    document.querySelectorAll('.settings-section input[type="text"], .settings-section input[type="password"]').forEach(input => {
        input.addEventListener('focus', () => {
            input.select();
        });
    });

    // ─── Loading state on form submit ───
    document.querySelectorAll('.form-card').forEach(form => {
        // Capture which button was clicked before submit
        let clickedBtn = null;
        form.querySelectorAll('button[type="submit"]').forEach(btn => {
            btn.addEventListener('click', () => { clickedBtn = btn; });
        });

        form.addEventListener('submit', () => {
            // Preserve clicked button's value via hidden input
            if (clickedBtn && clickedBtn.name) {
                const hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = clickedBtn.name;
                hidden.value = clickedBtn.value;
                form.appendChild(hidden);
            }
            const btns = form.querySelectorAll('button[type="submit"]');
            btns.forEach(btn => {
                btn.disabled = true;
                btn.textContent = 'Đang xử lý...';
            });
        });
    });
});
