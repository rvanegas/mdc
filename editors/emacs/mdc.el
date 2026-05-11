;;; mdc.el --- Minor mode for MDC conversation transcript files  -*- lexical-binding: t -*-

;; Version: 0.1.0
;; Package-Requires: ((emacs "27.1"))
;; Keywords: convenience tools markdown

;;; Commentary:
;; Support for MDC-format markdown conversation files.  Adds syntax
;; highlighting for dates, section headers, reference lines, and edit
;; directives; commands to invoke the mdc CLI (reply, fix, check,
;; validate, new); and navigation between turns.
;;
;; Quickstart:
;;   (require 'mdc)
;;   (add-hook 'find-file-hook #'mdc-maybe-enable)
;;
;; With markdown-mode:
;;   (add-hook 'markdown-mode-hook #'mdc-maybe-enable)

;;; Code:

(require 'seq)

(defgroup mdc nil
  "Support for MDC conversation transcript files."
  :group 'tools
  :prefix "mdc-")

(defcustom mdc-executable "mdc"
  "Path to the mdc executable."
  :type 'string
  :group 'mdc)

(defcustom mdc-revert-after-modify t
  "Revert the buffer automatically after `mdc-reply' or `mdc-fix' succeeds."
  :type 'boolean
  :group 'mdc)

;; ── Faces ────────────────────────────────────────────────────────────────────

(defface mdc-date-face
  '((t :inherit font-lock-constant-face))
  "Face for the yyyy-mm-dd date line in an MDC preamble."
  :group 'mdc)

(defface mdc-speaker-face
  '((t :inherit font-lock-keyword-face :weight bold))
  "Face for the speaker name in an MDC section header (## Speaker)."
  :group 'mdc)

(defface mdc-reference-face
  '((t :inherit font-lock-string-face))
  "Face for MDC reference lines (| Author (year) *Title*)."
  :group 'mdc)

;; ── Font-lock ────────────────────────────────────────────────────────────────

(defconst mdc--font-lock-keywords
  '(;; Date line: yyyy-mm-dd standing alone on a line.
    ("^[0-9]\\{4\\}-[0-9]\\{2\\}-[0-9]\\{2\\}$"
     . 'mdc-date-face)
    ;; Section headers: ## Speaker — highlight the speaker name.
    ("^##[[:space:]]+\\(.+\\)$"
     (1 'mdc-speaker-face))
    ;; Reference lines: | Author (year) *Title*
    ("^|[[:space:]].+$"
     . 'mdc-reference-face)
)
  "MDC-specific font-lock keywords.")

;; ── Keymap ───────────────────────────────────────────────────────────────────

(defvar mdc-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "C-c C-r") #'mdc-reply)
    (define-key map (kbd "C-c C-f") #'mdc-fix)
    (define-key map (kbd "C-c C-k") #'mdc-check)
    (define-key map (kbd "C-c C-v") #'mdc-validate)
    (define-key map (kbd "C-c C-n") #'mdc-new)
    (define-key map (kbd "M-n")     #'mdc-next-turn)
    (define-key map (kbd "M-p")     #'mdc-prev-turn)
    (define-key map (kbd "C-c C-e") #'mdc-goto-input)
    map)
  "Keymap for `mdc-mode'.")

;; ── Internal helpers ─────────────────────────────────────────────────────────

(defun mdc--require-file ()
  "Signal an error if the current buffer has no associated file."
  (unless (buffer-file-name)
    (user-error "Buffer is not visiting a file")))

(defun mdc--output-buffer ()
  "Return the *mdc* output buffer, erased and ready."
  (let ((buf (get-buffer-create "*mdc*")))
    (with-current-buffer buf
      (let ((inhibit-read-only t)) (erase-buffer)))
    buf))

(defun mdc--run-async (args &optional sentinel)
  "Start mdc with ARGS, streaming output to *mdc*.
Save the current buffer first.  SENTINEL, if given, is called with
\(process event) on exit.  Returns the process object."
  (mdc--require-file)
  (save-buffer)
  (let* ((file     (buffer-file-name))
         (dir      (file-name-directory file))
         (basename (file-name-nondirectory file))
         (buf      (mdc--output-buffer))
         (cmd      (cons mdc-executable (append args (list basename)))))
    (with-current-buffer buf
      (setq default-directory dir))
    (display-buffer buf)
    (let ((proc (apply #'start-process "mdc" buf cmd)))
      (when sentinel
        (set-process-sentinel proc sentinel))
      proc)))

(defun mdc--revert-sentinel (source-buf)
  "Return a process sentinel that reverts SOURCE-BUF on success."
  (lambda (proc _event)
    (when (and (zerop (process-exit-status proc))
               (buffer-live-p source-buf))
      (with-current-buffer source-buf
        (revert-buffer t t t)))))

;; ── Commands ─────────────────────────────────────────────────────────────────

;;;###autoload
(defun mdc-reply ()
  "Run `mdc reply' on the current file and revert when done."
  (interactive)
  (mdc--run-async
   '("reply")
   (when mdc-revert-after-modify
     (mdc--revert-sentinel (current-buffer)))))

;;;###autoload
(defun mdc-fix ()
  "Run `mdc fix' on the current file and revert when done."
  (interactive)
  (mdc--run-async
   '("fix")
   (when mdc-revert-after-modify
     (mdc--revert-sentinel (current-buffer)))))

;;;###autoload
(defun mdc-check ()
  "Run `mdc check' on the current file."
  (interactive)
  (mdc--run-async '("check")))

;;;###autoload
(defun mdc-validate ()
  "Run `mdc validate' on the current file."
  (interactive)
  (mdc--run-async '("validate")))

;;;###autoload
(defun mdc-new (title)
  "Create a new MDC transcript with TITLE in the current directory.
Opens the created file(s) for editing."
  (interactive "sNew transcript title: ")
  (let* ((dir (or (and (buffer-file-name)
                       (file-name-directory (buffer-file-name)))
                  default-directory))
         (created-files nil))
    (with-temp-buffer
      (let ((default-directory dir))
        (call-process mdc-executable nil t nil "new" title))
      (setq created-files
            (seq-filter (lambda (l) (string-suffix-p ".md" l))
                        (split-string (buffer-string) "\n" t))))
    (if (null created-files)
        (message "mdc new: no file created")
      (dolist (f (reverse created-files))
        (find-file (expand-file-name f dir))))))

;; ── Navigation ───────────────────────────────────────────────────────────────

;;;###autoload
(defun mdc-next-turn ()
  "Move point to the next `## Speaker' heading."
  (interactive)
  (let ((origin (point)))
    (end-of-line)
    (if (re-search-forward "^## " nil t)
        (beginning-of-line)
      (goto-char origin)
      (message "No more turns"))))

;;;###autoload
(defun mdc-prev-turn ()
  "Move point to the previous `## Speaker' heading."
  (interactive)
  (let ((origin (point)))
    (beginning-of-line)
    (if (re-search-backward "^## " nil t)
        (beginning-of-line)
      (goto-char origin)
      (message "No previous turn"))))

(defun mdc--ai-speaker-p (speaker)
  "Return non-nil if SPEAKER is a known AI assistant name."
  (member (downcase speaker) '("claude" "gpt" "ollama")))

(defun mdc--turn-speaker-p (speaker)
  "Return non-nil if SPEAKER is a conversational turn (not a structural section)."
  (not (member (downcase speaker) '("references" "related"))))

(defun mdc--last-turn ()
  "Return (SPEAKER . HEADER-POS) for the last conversational turn, or nil.
Skips structural sections (References, Related)."
  (save-excursion
    (goto-char (point-max))
    (let (result)
      (while (and (not result)
                  (re-search-backward "^## \\(.+\\)$" nil t))
        (let ((spk (match-string-no-properties 1)))
          (when (mdc--turn-speaker-p spk)
            (setq result (cons spk (match-beginning 0))))))
      result)))

(defun mdc--structural-sections-start ()
  "Return the buffer position just before any trailing structural sections.
Structural sections (References, Related) must be at the end of the file.
Returns `point-max' if none are present."
  (save-excursion
    (goto-char (point-max))
    (let ((pos (point-max)))
      (while (re-search-backward "^## \\(.+\\)$" nil t)
        (if (not (mdc--turn-speaker-p (match-string-no-properties 1)))
            (setq pos (match-beginning 0))
          (goto-char (point-min))))  ; stop scanning
      pos)))

(defun mdc--prev-user-speaker ()
  "Return the speaker name from the most recent user (non-AI, non-structural) turn, or nil."
  (save-excursion
    (goto-char (point-max))
    (let (result)
      (while (and (not result)
                  (re-search-backward "^## \\(.+\\)$" nil t))
        (let ((spk (match-string-no-properties 1)))
          (when (and (mdc--turn-speaker-p spk)
                     (not (mdc--ai-speaker-p spk)))
            (setq result spk))))
      result)))

;;;###autoload
(defun mdc-goto-input ()
  "Navigate to the next input position in the transcript.

If the last turn is a user turn, move point to the end of its body.
If the last turn is an AI turn, append a new user-turn section using
the speaker name from the previous user turn, then place point on the
blank line that follows the new section header."
  (interactive)
  (let* ((last    (mdc--last-turn))
         (speaker (car last))
         (hdr-pos (cdr last)))
    (unless last
      (user-error "No turns found in buffer"))
    (if (not (mdc--ai-speaker-p speaker))
        ;; User turn — jump to the end of its body.
        (let ((body-end
               (save-excursion
                 (goto-char hdr-pos)
                 (forward-line 1)
                 (if (re-search-forward "^## " nil t)
                     (progn (beginning-of-line)
                            (skip-chars-backward " \t\n")
                            (point))
                   (goto-char (point-max))
                   (skip-chars-backward " \t\n")
                   (point)))))
          (goto-char body-end)
          (end-of-line))
      ;; AI turn — append a fresh user section and land on its body line.
      (let ((user-spk (or (mdc--prev-user-speaker)
                          (user-error "No previous user turn found"))))
        (let ((insert-pos (mdc--structural-sections-start)))
          (goto-char insert-pos)
          (skip-chars-backward "\n")
          (insert "\n\n## " user-spk "\n\n")
          (when (eobp)
            (forward-line -1)))))))

;; ── Auto-detection ───────────────────────────────────────────────────────────

(defun mdc--dated-slug-p (filename)
  "Return non-nil if FILENAME matches the MDC dated-slug convention.
Matches yyyy-mm-dd-*.md, yyyy-mm-dd-*.chat.md, etc."
  (string-match-p "\\`[0-9]\\{4\\}-[0-9]\\{2\\}-[0-9]\\{2\\}-[^/]+" filename))

;;;###autoload
(defun mdc-maybe-enable ()
  "Enable `mdc-mode' if the buffer's filename matches the MDC convention.
Add this to `find-file-hook' or `markdown-mode-hook'."
  (let ((name (file-name-nondirectory (or (buffer-file-name) ""))))
    (when (mdc--dated-slug-p name)
      (mdc-mode 1))))

;; ── Minor mode ───────────────────────────────────────────────────────────────

;;;###autoload
(define-minor-mode mdc-mode
  "Minor mode for MDC conversation transcript files.

Adds syntax highlighting for dates, section headers, reference lines,
and edit directives.  Provides commands to invoke the mdc CLI and
navigate between conversation turns.

Key bindings:
\\{mdc-mode-map}"
  :lighter " MDC"
  :keymap mdc-mode-map
  (if mdc-mode
      (progn
        (font-lock-add-keywords nil mdc--font-lock-keywords 'append)
        (auto-revert-mode 1))
    (font-lock-remove-keywords nil mdc--font-lock-keywords))
  (font-lock-flush))

(provide 'mdc)
;;; mdc.el ends here
