var currentPages = [];
    var currentIndex = 0;
    var currentLang = 'en';
    var isSpeaking = false;
    var selectedPhotoFile = null;
    var currentBookTitle = "Magical Story";
    var isPaidUser = false;
    var currentDedicationText = "";
    var currentPsychologyQuestions = [];
    var pendingFormatAction = null;
    var authMode = 'login';
    var currentUser = null;

    function setText(id, val) {
        var el = document.getElementById(id);
        if (el && val !== undefined) el.innerText = val;
    }
    function setHtml(id, val) {
        var el = document.getElementById(id);
        if (el && val !== undefined) el.innerHTML = val;
    }

    function switchTab(tabName) {
        document.querySelectorAll('.tab-content').forEach(function(el) { el.classList.remove('active'); });
        document.querySelectorAll('.tab-btn').forEach(function(el) { el.classList.remove('active'); });
        
        var targetTab = document.getElementById('tab-' + tabName);
        if (targetTab) targetTab.classList.add('active');
        
        var navBtn = document.getElementById('nav-' + tabName);
        if (navBtn) navBtn.classList.add('active');

        if (tabName === 'library') loadLibrary();
    }

    /* --- УПРАВЛЕНИЕ МОДАЛКОЙ ВЫБОРА ФОРМАТА --- */
    function openFormatModal() {
        var consent = document.getElementById('legalConsent');
        var d = dictionary[currentLang] || dictionary['en'];
        if (consent && !consent.checked) {
            alert(d.consentAlert);
            return;
        }
        var modal = document.getElementById('formatModal');
        if (modal) modal.style.display = 'flex';
    }

    function closeFormatModal() {
        var modal = document.getElementById('formatModal');
        if (modal) modal.style.display = 'none';
    }

    async function handleFormatSelection(formatType) {
        closeFormatModal();

        if (formatType === 'digital' && !isPaidUser) {
            pendingFormatAction = 'digital';
            openPaymentModal();
            return;
        }

        var success = await generateStory();
        if (!success) return;

        if (formatType === 'read') {
            showSlide(0);
        } else if (formatType === 'digital') {
            exportEpub();
        } else if (formatType === 'hardcover') {
            switchTab('order');
        }
    }

    /* --- УПРАВЛЕНИЕ МОДАЛКОЙ АВТОРИЗАЦИИ / РЕГИСТРАЦИИ --- */
    function openAuthModal() {
        if (currentUser) {
            var confirmLogout = confirm(currentLang === 'ru' 
                ? ('Вы вошли как ' + currentUser.email + '. Хотите выйти?') 
                : ('Signed in as ' + currentUser.email + '. Would you like to sign out?'));
            if (confirmLogout) {
                currentUser = null;
                localStorage.removeItem('storymagic_user');
                updateAuthButtonUI();
            }
            return;
        }
        var modal = document.getElementById('authModal');
        if (modal) modal.style.display = 'flex';
    }

    function closeAuthModal() {
        var modal = document.getElementById('authModal');
        if (modal) modal.style.display = 'none';
    }

    function switchAuthMode(mode) {
        authMode = mode;
        var d = dictionary[currentLang] || dictionary['en'];
        var btnLogin = document.getElementById('tab-btn-login');
        var btnReg = document.getElementById('tab-btn-register');
        var nameGroup = document.getElementById('auth-name-group');
        var submitBtn = document.getElementById('btn-auth-submit');

        if (mode === 'login') {
            if (btnLogin) btnLogin.classList.add('active');
            if (btnReg) btnReg.classList.remove('active');
            if (nameGroup) nameGroup.style.display = 'none';
            if (submitBtn) submitBtn.innerText = d.authSignIn || 'Sign In';
        } else {
            if (btnLogin) btnLogin.classList.remove('active');
            if (btnReg) btnReg.classList.add('active');
            if (nameGroup) nameGroup.style.display = 'block';
            if (submitBtn) submitBtn.innerText = d.authRegister || 'Create Account';
        }
    }

    async function submitAuthForm(e) {
        e.preventDefault();
        var nameVal = (document.getElementById('auth-name-input').value || '').trim();
        var emailVal = (document.getElementById('auth-email-input').value || '').trim();
        var passVal = (document.getElementById('auth-pass-input').value || '').trim();

        if (!emailVal || !passVal) return;

        currentUser = { name: nameVal || emailVal.split('@')[0], email: emailVal };
        localStorage.setItem('storymagic_user', JSON.stringify(currentUser));
        
        var orderEmail = document.getElementById('order-email-input');
        if (orderEmail) orderEmail.value = currentUser.email;
        var orderName = document.getElementById('order-name-input');
        if (orderName && nameVal) orderName.value = nameVal;

        updateAuthButtonUI();
        closeAuthModal();
    }

    function updateAuthButtonUI() {
        var d = dictionary[currentLang] || dictionary['en'];
        var btn = document.getElementById('btn-auth');
        if (!btn) return;
        if (currentUser && currentUser.name) {
            btn.innerText = '👤 ' + currentUser.name;
        } else {
            btn.innerText = d.btnAuth || '👤 Sign In / Register';
        }
    }

    /* --- МОДАЛКА ОПЛАТЫ --- */
    function openPaymentModal() { 
        var modal = document.getElementById('paymentModal');
        if (modal) modal.style.display = 'flex'; 
    }

    function closePaymentModal() { 
        var modal = document.getElementById('paymentModal');
        if (modal) modal.style.display = 'none'; 
    }

    function closeModalOnBackdrop(e) {
        if (!e.target) return;
        if (e.target.id === 'paymentModal') closePaymentModal();
        if (e.target.id === 'formatModal') closeFormatModal();
        if (e.target.id === 'authModal') closeAuthModal();
    }

    async function activatePaidAccess(planName) {
        isPaidUser = true;
        closePaymentModal();
        updateSlideUI();
        alert(currentLang === 'ru' 
            ? ('Тариф ' + planName + ' активирован! Финальная глава и скачивание ePub разблокированы.') 
            : (planName + ' plan activated! Final chapter and ePub export are now unlocked.'));

        if (pendingFormatAction === 'digital') {
            pendingFormatAction = null;
            var ok = await generateStory();
            if (ok) exportEpub();
        }
    }

    function handlePhotoSelect(e) {
        var file = e.target.files[0];
        if (!file) return;
        selectedPhotoFile = file;
        var reader = new FileReader();
        reader.onload = function(evt) {
            var thumb = document.getElementById('photoThumb');
            if (thumb) {
                thumb.src = evt.target.result;
                thumb.style.display = 'block';
            }
            setText('photoStatusText', file.name);
        };
        reader.readAsDataURL(file);
    }

    function checkCustomTheme() {
        var sel = document.getElementById('themeSelect');
        var box = document.getElementById('customThemeBox');
        if (sel && box) box.style.display = (sel.value === 'custom') ? 'block' : 'none';
    }

    function updateSlideUI() {
        if (!currentPages || currentPages.length === 0) return;
        var p = currentPages[currentIndex];
        var d = dictionary[currentLang] || dictionary['en'];

        var isLastPage = (currentPages.length > 1) && (currentIndex === currentPages.length - 1);
        var isLocked = isLastPage && !isPaidUser;

        var img = document.getElementById('currentImage');
        if (img) {
            img.style.opacity = '0.3';
            var temp = new Image();
            temp.onload = function() {
                img.src = temp.src;
                img.style.opacity = '1';
            };
            temp.src = p.image_url;

            if (isLocked) img.classList.add('locked-blur');
            else img.classList.remove('locked-blur');
        }

        var overlay = document.getElementById('paywallLockOverlay');
        if (overlay) overlay.style.display = isLocked ? 'flex' : 'none';

        var pageNum = p.page_number || (currentIndex + 1);
        setText('pageBadge', (currentIndex === 0) ? d.badgeCover : (d.badgePage + ' ' + pageNum));
        setText('pageCounter', pageNum + ' / ' + currentPages.length);
        
        var textContainer = document.getElementById('currentText');
        if (textContainer) {
            textContainer.innerText = p.text;
            textContainer.scrollTop = 0;
        }
    }

    function showSlide(idx) {
        if (!currentPages || currentPages.length === 0) return;
        currentIndex = idx;
        updateSlideUI();
    }

    function nextSlide() {
        if (!currentPages.length) return;
        showSlide(currentIndex < currentPages.length - 1 ? currentIndex + 1 : 0);
    }

    function prevSlide() {
        if (!currentPages.length) return;
        showSlide(currentIndex > 0 ? currentIndex - 1 : currentPages.length - 1);
    }

    async function generateStory() {
        var consent = document.getElementById('legalConsent');
        var d = dictionary[currentLang] || dictionary['en'];
        if (consent && !consent.checked) {
            alert(d.consentAlert);
            return false;
        }

        setText('statusMsg', d.statusGen);

        var themeEl = document.getElementById('themeSelect');
        var selectedTheme = themeEl ? themeEl.options[themeEl.selectedIndex].text : 'Magical tale';
        if (themeEl && themeEl.value === 'custom') {
            var customInput = document.getElementById('customThemeInput');
            selectedTheme = (customInput && customInput.value.trim()) ? customInput.value.trim() : 'Custom gentle fairy tale';
        }

        var heroInput = document.getElementById('heroName');
        var heroNameVal = (heroInput && heroInput.value.trim()) ? heroInput.value.trim() : 'Hero';
        currentBookTitle = heroNameVal + ' - ' + selectedTheme;

        var dedicationInput = document.getElementById('customDedication');
        var dedicationVal = dedicationInput ? dedicationInput.value.trim() : '';

        var formData = new FormData();
        formData.append('hero_name', heroNameVal);
        formData.append('age', document.getElementById('childAge').value || 7);
        formData.append('theme', selectedTheme);
        formData.append('style', document.getElementById('artStyle').value);
        formData.append('lang', currentLang);
        if (dedicationVal) {
            formData.append('custom_dedication', dedicationVal);
        }

        if (selectedPhotoFile) {
            formData.append('photo', selectedPhotoFile);
        }

        try {
            var response = await fetch('/api/generate', { method: 'POST', body: formData });
            var result = await response.json();
            if (result.success && result.pages) {
                currentPages = result.pages;
                if (result.title) currentBookTitle = result.title;
                currentDedicationText = result.dedication_text || dedicationVal;
                currentPsychologyQuestions = result.psychology_questions || [];
                setText('statusMsg', d.statusDone);
                showSlide(0);
                saveBookToLibrary(heroNameVal, selectedTheme, currentPages, currentDedicationText, currentPsychologyQuestions);
                return true;
            } else {
                setText('statusMsg', '❌ ' + (result.error || 'Error'));
                return false;
            }
        } catch (e) {
            setText('statusMsg', '❌ ' + e.message);
            return false;
        }
    }

    function saveBookToLibrary(hero, theme, pages, dedication, questions) {
        try {
            var lib = JSON.parse(localStorage.getItem('storymagic_lib') || '[]');
            lib.unshift({
                id: Date.now(),
                hero: hero,
                theme: theme,
                cover: pages[0].image_url,
                pages: pages,
                dedication_text: dedication,
                psychology_questions: questions,
                date: new Date().toLocaleDateString()
            });
            localStorage.setItem('storymagic_lib', JSON.stringify(lib.slice(0, 10)));
        } catch (e) {}
    }

    function loadLibrary() {
        var box = document.getElementById('libraryContainer');
        if (!box) return;
        var lib = JSON.parse(localStorage.getItem('storymagic_lib') || '[]');
        var d = dictionary[currentLang] || dictionary['en'];
        box.innerHTML = '';
        if (lib.length === 0) {
            box.innerHTML = '<p style="color:#aaa;">' + d.libEmpty + '</p>';
            return;
        }
        lib.forEach(function(book) {
            var el = document.createElement('div');
            el.className = 'book-item-card';
            el.innerHTML = '<img class="book-item-thumb" src="' + book.cover + '" alt="' + book.hero + '">' +
                           '<div class="book-item-info">' +
                           '<div class="book-item-title">' + book.hero + ': ' + book.theme + '</div>' +
                           '<div class="book-item-meta">' + book.date + ' • ' + book.pages.length + ' p.</div>' +
                           '</div>';
            el.onclick = function() {
                currentPages = book.pages;
                currentBookTitle = book.hero + ' - ' + book.theme;
                currentDedicationText = book.dedication_text || '';
                currentPsychologyQuestions = book.psychology_questions || [];
                switchTab('create');
                showSlide(0);
            };
            box.appendChild(el);
        });
    }

    var currentAudio = null;

    async function toggleAudioStory() {
        if (!currentPages || currentPages.length === 0) return;
        if (currentPages.length > 1 && currentIndex === currentPages.length - 1 && !isPaidUser) {
            openPaymentModal();
            return;
        }

        if (currentAudio) {
            currentAudio.pause();
            currentAudio = null;
            isSpeaking = false;
            return;
        }

        var text = currentPages[currentIndex].text;
        var btnAudio = document.getElementById('btn-audio-lbl');
        var originalLabel = btnAudio ? btnAudio.innerText : 'Audio Story';
        if (btnAudio) btnAudio.innerText = '⏳ Loading Voice...';

        try {
            var response = await fetch('/api/tts', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: text, lang: currentLang })
            });

            if (!response.ok) throw new Error('TTS generation failed');

            var blob = await response.blob();
            var audioUrl = window.URL.createObjectURL(blob);
            currentAudio = new Audio(audioUrl);
            
            isSpeaking = true;
            if (btnAudio) btnAudio.innerText = '⏹ Stop Audio';

            currentAudio.play();
            
            currentAudio.onended = function() {
                isSpeaking = false;
                currentAudio = null;
                if (btnAudio) btnAudio.innerText = originalLabel;
            };
        } catch (err) {
            alert('Audio Error: ' + err.message);
            if (btnAudio) btnAudio.innerText = originalLabel;
            isSpeaking = false;
        }
    }   

    async function exportEpub() {
        if (!currentPages || currentPages.length === 0) {
            alert(currentLang === 'ru' ? 'Сначала создайте книгу!' : 'Please generate a book first!');
            return;
        }
        if (!isPaidUser) {
            openPaymentModal();
            return;
        }

        var btn = document.getElementById('btn-epub-lbl');
        var originalText = btn ? btn.innerText : 'Export ePub';
        if (btn) btn.innerText = '⏳ ...';

        var d = dictionary[currentLang] || dictionary['en'];
        var chapters = currentPages.map(function(page, index) {
            var paragraphs = page.text.split('\n\n').filter(Boolean);
            return {
                chapter_number: index + 1,
                title: page.title || ((d.badgePage || 'Chapter') + ' ' + (index + 1)),
                paragraphs: paragraphs.length ? paragraphs : [page.text],
                image_url: page.image_url
            };
        });

        var payload = {
            title: currentBookTitle || 'StoryMagic Book',
            language_code: currentLang,
            author: 'StoryMagic Hub',
            description: 'Personalized therapeutic children book',
            dedication_text: currentDedicationText || '',
            psychology_questions: currentPsychologyQuestions || [],
            chapters: chapters,
            is_paid: isPaidUser
        };

        try {
            var response = await fetch('/api/export-epub', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (response.status === 402) {
                openPaymentModal();
                return;
            }
            if (!response.ok) throw new Error('ePub export failed');

            var blob = await response.blob();
            var downloadUrl = window.URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = downloadUrl;
            var safeName = (currentBookTitle || 'Story').replace(/[^a-zA-Z0-9а-яА-ЯёЁ]/g, '_');
            a.download = safeName + '.epub';
            document.body.appendChild(a);
            a.click();
            a.remove();
            window.URL.revokeObjectURL(downloadUrl);
        } catch (err) {
            alert('Export Error: ' + err.message);
        } finally {
            if (btn) btn.innerText = originalText;
        }
    }

    async function submitLuluOrder() {
        if (!currentPages || currentPages.length === 0) {
            alert(currentLang === 'ru' ? 'Сначала сгенерируйте книгу!' : 'Please generate a book first!');
            return;
        }

        var nameVal = (document.getElementById('order-name-input').value || '').trim() || 'Customer';
        var emailVal = (document.getElementById('order-email-input').value || '').trim();
        var phoneVal = (document.getElementById('order-phone-input').value || '').trim();
        var street1Val = (document.getElementById('order-addr-input').value || '').trim();
        var street2Val = (document.getElementById('order-addr2-input').value || '').trim();
        var cityVal = (document.getElementById('order-city-input').value || '').trim();
        var postcodeVal = (document.getElementById('order-postcode-input').value || '').trim();
        var countryVal = (document.getElementById('order-country-input').value || 'GB').trim();

        if (!emailVal || !phoneVal || !street1Val || !cityVal || !postcodeVal) {
            alert(currentLang === 'ru' 
                ? 'Пожалуйста, заполните все обязательные поля: Email, телефон, улицу, город и индекс.' 
                : 'Please fill in all required fields: Email, Phone, Street, City, and Postcode.');
            return;
        }

        var safeStreet1 = street1Val.substring(0, 30);
        var safeStreet2 = street2Val.substring(0, 30);

        var chapters = currentPages.map(function(page, index) {
            var paragraphs = page.text.split('\n\n').filter(Boolean);
            return {
                chapter_number: index + 1,
                title: page.title || ('Chapter ' + (index + 1)),
                paragraphs: paragraphs.length ? paragraphs : [page.text],
                image_url: page.image_url
            };
        });

        var storyBookData = {
            title: currentBookTitle || 'StoryMagic Book',
            language_code: currentLang,
            author: 'StoryMagic Hub',
            description: 'Personalized therapeutic children book',
            dedication_text: currentDedicationText || '',
            psychology_questions: currentPsychologyQuestions || [],
            chapters: chapters,
            is_paid: true
        };

        var shippingAddress = {
            name: nameVal,
            street1: safeStreet1,
            city: cityVal,
            postcode: postcodeVal,
            country_code: countryVal,
            phone_number: phoneVal
        };
        if (safeStreet2) {
            shippingAddress.street2 = safeStreet2;
        }

        var payload = {
            story: storyBookData,
            contact_email: emailVal,
            shipping_address: shippingAddress
        };

        var btnOrder = document.getElementById('btn-order-pay');
        var origBtnText = btnOrder ? btnOrder.innerText : 'Proceed to Payment';
        if (btnOrder) btnOrder.innerText = '⏳ Sending to Lulu...';

        try {
            var response = await fetch('/api/lulu-create-order', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            var result = await response.json();
            if (response.ok && result.success) {
                alert('🎉 Заказ успешно отправлен в Sandbox Lulu! ID заказа: ' + (result.lulu_response.id || 'создан'));
                console.log('Lulu response:', result.lulu_response);
            } else {
                alert('Ошибка при оформлении заказа: ' + (result.detail || 'Неизвестная ошибка'));
            }
        } catch (err) {
            console.error(err);
            alert('Произошла ошибка соединения с сервером.');
        } finally {
            if (btnOrder) btnOrder.innerText = origBtnText;
        }
    }

    var dictionary = {
        en: {
            navAbout: 'Home / About', navCreate: 'Create Story', navLibrary: 'My Books', navOrder: 'Order Hardcover', btnAuth: '👤 Sign In / Register',
            heroTitle: 'Personalized Therapeutic Books for Children',
            heroDesc: 'Upload your child\'s photo, and we will craft a gentle, inspiring story where a hero resembling your child conquers fears and discovers superpowers.',
            heroCta: '🚀 Start Creating',
            step1Title: '1. Photo & Hero', step1Desc: 'Upload a picture and enter child\'s name for tailored character resemblance.',
            step2Title: '2. Gentle Narrative', step2Desc: 'Choose therapeutic themes: dark fears, first day of school, anger, sharing.',
            step3Title: '3. Read or Order', step3Desc: 'Read online, download as an ePub file, or order a hardcover print book.',
            aboutTitle: 'About StoryMagic', aboutTagline: 'Personalized therapeutic tales helping children grow confident, resilient, and happy.',
            aboutCol1Title: 'Why Story Therapy?', aboutCol1Desc: 'Each story is uniquely crafted for your child. Story therapy gently resolves childhood fears, builds emotional intelligence, and encourages kindness.',
            aboutCol2Title: 'Safety & Privacy', aboutCol2Desc: 'Fully compliant with UK GDPR & the ICO Children\'s Code. Photos are strictly used to create illustrations, never train public models, and are immediately deleted.',
            aboutCol3Title: 'Formats & Print', aboutCol3Desc: 'Read on screen, export as an ePub file for tablet reading (iPad/Android), or order a beautifully bound physical hardcover delivered to your door.',
            aboutStepsTitle: '3 Simple Steps:',
            aboutStep1: '<strong>Step 1:</strong> Enter child details and choose an inspiring story theme.',
            aboutStep2: '<strong>Step 2:</strong> Upload an optional photo & confirm parental consent.',
            aboutStep3: '<strong>Step 3:</strong> Enjoy a unique, beautifully illustrated storybook in minutes!',
            lblFormTitle: 'Book Settings', lblPhoto: '📷 Child Photo (for likeness)', photoChoose: 'Click to select photo',
            lblDedication: '❤️ Parental Dedication (Optional)',
            lblHeroName: 'Hero Name', valHeroName: 'Alice', lblHeroAge: 'Age', lblStoryTheme: 'Story Theme',
            grpTherapy: '🌱 Therapeutic Stories', themeDark: 'Fear of the dark & night shadows', themeSchool: 'First day at nursery / school',
            themeAnger: 'Anger, tantrums & big feelings', themeToys: 'Tidying up toys & order', themeShare: 'Kindness, sharing & making friends',
            grpAdv: '🚀 Magical Adventures', themeForest: 'Enchanted forest & wise talking cat', themeSpace: 'Space voyage to the Star Island',
            themeDino: 'Expedition to the gentle dinosaurs', optCustom: '✍️ Custom Story Idea...',
            lblArtStyle: 'Art Style', optStyle1: 'Watercolor / Fairy Tale', optStyle2: '3D Animation', optStyle3: 'Classic Gouache',
            lblConsentText: 'I confirm that I am the parent or legal guardian and consent to photo processing for book illustration. Photos are deleted immediately and never shared publicly.',
            consentAlert: 'Please confirm parental/guardian consent to proceed.', btnCreateStory: '🪄 Generate Book',
            badgeCover: 'Chapter 1', badgePage: 'Chapter',
            defaultWelcome: 'Fill in the parameters on the left and click "Generate Book". The storybook will unfold here in a full two-page spread with large, child-friendly typography.',
            btnPrev: '❮ Back', btnNext: 'Next ❯', btnAudioLbl: 'Audio Story', btnEpubLbl: 'Export ePub', btnOrderLbl: 'Order Hardcover',
            lockTitle: 'Final Chapter Locked', lockDesc: 'Unlock the magical finale of this story, full audio narration, and ePub download!', lockBtn: '✨ Unlock Full Book',
            libTitle: '📚 Your Created Books', libDesc: 'History of all stories created on this device.', libEmpty: 'No stories saved yet. Create your first book!',
            orderTitle: '🚚 Order a Printed Hardcover Book', orderDesc: 'We print your story on premium 170 gsm glossy paper with vibrant full-bleed illustrations and deliver to your doorstep.',
            lblOrderName: 'Recipient Full Name', lblOrderEmail: 'Contact Email', lblOrderPhone: 'Phone Number (Required)',
            lblOrderCountry: 'Country', lblOrderAddr: 'Street Address Line 1 (Max 30 chars)', lblOrderAddr2: 'Apt / Suite / Line 2 (Optional)',
            lblOrderCity: 'City / Town', lblOrderPostcode: 'Postcode / ZIP', lblOrderType: 'Cover Type',
            optOrderHard: 'Matte Hardcover — £14.99', optOrderPrem: 'Premium Gloss with Foil — £19.99', btnOrderPay: 'Proceed to Payment / Sandbox Order',
            payTitle: 'Choose Your Plan', paySubtitle: 'Unlock the final chapter, ePub downloads, and voice narration',
            plan1Title: 'Starter', plan1Desc: '5 personalized stories + ePub', plan1Btn: 'Buy Now',
            plan2Title: 'Unlimited', plan2Desc: 'Unlimited stories + ePub exports', plan2Btn: 'Select Plan',
            fmtModalTitle: '✨ Choose Your Book Edition', fmtModalSubtitle: 'How would you like to experience your child\'s personalized story?',
            fmtCard1Title: 'Read Online', fmtCard1Desc: 'Interactive two-page book spread right in your browser with free preview.', fmtCard1Price: 'Free Preview', fmtCard1Btn: 'Read Now',
            fmtCard2Title: 'Digital eBook', fmtCard2Desc: 'Full story unlocked, Apple Books / Kindle ePub download & voice narration.', fmtCard2Price: 'From £4.99', fmtCard2Btn: 'Get eBook',
            fmtBadgeHit: 'Most Popular', fmtCard3Title: 'Printed Book', fmtCard3Desc: 'High-quality physical book printed & delivered directly to your door via Lulu.', fmtCard3Price: '£14.99 + Shipping', fmtCard3Btn: 'Order Print',
            authModalTitle: 'Account Access', authModalSubtitle: 'Save your generated books and track print orders across all devices.',
            authSignIn: 'Sign In', authRegister: 'Create Account', lblAuthName: 'Your Name', lblAuthEmail: 'Email Address', lblAuthPass: 'Password',
            statusGen: '✨ Creating your personal story and illustrations...', statusDone: '🎉 Book is ready!'
        },
        ru: {
            navAbout: 'Главная', navCreate: 'Создать сказку', navLibrary: 'Мои книжки', navOrder: 'Заказать печать', btnAuth: '👤 Войти / Регистрация',
            heroTitle: 'Персональные терапевтические книги для детей',
            heroDesc: 'Загрузите фото ребёнка, и мы создадим добрую поучительную сказку, в которой главный герой с его внешностью победит страхи и найдёт друзей.',
            heroCta: '🚀 Начать создание',
            step1Title: '1. Фотография и герой', step1Desc: 'Укажите имя и прикрепите фото — герой книги будет похож на вашего ребёнка.',
            step2Title: '2. Терапевтический сюжет', step2Desc: 'Выберите тему: страх темноты, первый день в садике, капризы или дружба.',
            step3Title: '3. Читайте или закажите', step3Desc: 'Читайте в книжном развороте, скачивайте в ePub или оформите в печатном переплете.',
            aboutTitle: 'О проекте StoryMagic', aboutTagline: 'Персонализированные сказки, помогающие детям расти уверенными и спокойными.',
            aboutCol1Title: 'В чем суть?', aboutCol1Desc: 'Каждая история создается индивидуально под вашего ребенка. Сказкотерапия помогает мягко преодолевать страхи, учит дружбе и вере в себя.',
            aboutCol2Title: 'Конфиденциальность', aboutCol2Desc: 'Мы строго соблюдаем UK GDPR и стандарты ICO Children\'s Code. Фотографии используются исключительно для генерации книги и сразу удаляются.',
            aboutCol3Title: 'Как получить книгу?', aboutCol3Desc: 'Читайте онлайн, сохраняйте в ePub для планшетов (iPad/Android) или заказывайте подарочное печатное издание в твердом переплете.',
            aboutStepsTitle: '3 простых шага:',
            aboutStep1: '<strong>Шаг 1:</strong> Укажите имя, возраст ребенка и выберите развивающую тему.',
            aboutStep2: '<strong>Шаг 2:</strong> Загрузите фото (по желанию) и подтвердите согласие родителя.',
            aboutStep3: '<strong>Шаг 3:</strong> Получите готовую иллюстрированную книгу за пару минут!',
            lblFormTitle: 'Параметры книги', lblPhoto: '📷 Фото ребёнка (для сходства)', photoChoose: 'Нажмите, чтобы выбрать фото',
            lblDedication: '❤️ Посвящение от родителей (необязательно)',
            lblHeroName: 'Имя героя', valHeroName: 'Алиса', lblHeroAge: 'Возраст', lblStoryTheme: 'Тема сказки',
            grpTherapy: '🌱 Терапевтические сказки', themeDark: 'Страх темноты и ночных теней', themeSchool: 'Первый день в садике / школе',
            themeAnger: 'Злость, капризы и эмоции', themeToys: 'Уборка игрушек и аккуратность', themeShare: 'Умение делиться и дружба',
            grpAdv: '🚀 Волшебные приключения', themeForest: 'Зачарованный лес и мудрый кот', themeSpace: 'Космическая экспедиция к звёздам',
            themeDino: 'Экспедиция в мир динозавров', optCustom: '✍️ Написать свою тему...',
            lblArtStyle: 'Стиль графики', optStyle1: 'Акварель / Сказка', optStyle2: '3D Мультфильм', optStyle3: 'Классическая гуашь',
            lblConsentText: 'Я являюсь родителем или опекуном ребёнка и даю согласие на обработку фото для создания книги. Фото удаляется сразу после обработки.',
            consentAlert: 'Пожалуйста, подтвердите согласие родителя/опекуна.', btnCreateStory: '🪄 Создать сказку',
            badgeCover: 'Глава 1', badgePage: 'Глава',
            defaultWelcome: 'Заполните параметры слева и нажмите «Создать сказку». Книга развернётся здесь в формате настоящего книжного разворота с удобным шрифтом.',
            btnPrev: '❮ Назад', btnNext: 'Вперед ❯', btnAudioLbl: 'Аудиосказка', btnEpubLbl: 'Скачать ePub', btnOrderLbl: 'Заказать книжку',
            lockTitle: 'Финальная глава закрыта', lockDesc: 'Разблокируйте волшебный финал сказки, озвучку и скачивание книги в формате ePub!', lockBtn: '✨ Разблокировать финал',
            libTitle: '📚 Ваши созданные книги', libDesc: 'История всех книг, сгенерированных на этом устройстве.', libEmpty: 'Пока нет сохраненных книг. Создайте первую сказку!',
            orderTitle: '🚚 Заказ книги в твердом переплете', orderDesc: 'Мы напечатаем сказку на премиальной глянцевой бумаге 170 г/м² с яркими иллюстрациями и доставим до двери.',
            lblOrderName: 'ФИО получателя', lblOrderEmail: 'Email для заказа', lblOrderPhone: 'Телефон (обязательно для курьера)',
            lblOrderCountry: 'Страна', lblOrderAddr: 'Улица и дом (до 30 символов)', lblOrderAddr2: 'Квартира / корпус (необязательно)',
            lblOrderCity: 'Город', lblOrderPostcode: 'Почтовый индекс (Postcode)', lblOrderType: 'Тип переплета',
            optOrderHard: 'Твердый матовый переплет — £14.99', optOrderPrem: 'Премиум глянец с тиснением — £19.99', btnOrderPay: 'Отправить заказ в Sandbox Lulu',
            payTitle: 'Выберите тариф', paySubtitle: 'Разблокируйте финал истории, скачивание ePub и озвучку',
            plan1Title: 'Starter', plan1Desc: '5 персональных сказок + ePub', plan1Btn: 'Купить',
            plan2Title: 'Unlimited', plan2Desc: 'Безлимит сказок + экспорт ePub', plan2Btn: 'Выбрать',
            fmtModalTitle: '✨ Выберите формат вашей книги', fmtModalSubtitle: 'В каком виде вы хотите получить персональную сказку?',
            fmtCard1Title: 'Читать на месте', fmtCard1Desc: 'Интерактивный книжный разворот прямо в браузере с бесплатным превью.', fmtCard1Price: 'Бесплатное превью', fmtCard1Btn: 'Открыть книгу',
            fmtCard2Title: 'Электронная книга', fmtCard2Desc: 'Полная версия сказки, файл ePub для планшетов и аудио-озвучка.', fmtCard2Price: 'От £4.99', fmtCard2Btn: 'Скачать ePub',
            fmtBadgeHit: 'Хит продаж', fmtCard3Title: 'Печатная книга', fmtCard3Desc: 'Настоящая книга в переплете с доставкой на дом через типографию Lulu.', fmtCard3Price: '£14.99 + доставка', fmtCard3Btn: 'Заказать печать',
            authModalTitle: 'Личный кабинет', authModalSubtitle: 'Сохраняйте созданные книги и историю заказов в базе данных.',
            authSignIn: 'Войти', authRegister: 'Регистрация', lblAuthName: 'Ваше имя', lblAuthEmail: 'Ваш Email', lblAuthPass: 'Пароль',
            statusGen: '✨ Создаем персональную сказку и волшебные иллюстрации...', statusDone: '🎉 Сказка готова!'
        },
        pl: {
            navAbout: 'Start / O nas', navCreate: 'Stwórz bajkę', navLibrary: 'Moje książki', navOrder: 'Zamów druk', btnAuth: '👤 Zaloguj się',
            heroTitle: 'Spersonalizowane bajki terapeutyczne dla dzieci', heroDesc: 'Prześlij zdjęcie dziecka, a stworzymy ciepłą opowieść, w której bohater pokonuje lęki.', heroCta: '🚀 Rozpocznij tworzenie',
            step1Title: '1. Zdjęcie i bohater', step1Desc: 'Podaj imię i dodaj zdjęcie dziecka.', step2Title: '2. Wątek terapeutyczny', step2Desc: 'Wybierz temat bajki terapeutycznej.', step3Title: '3. Czytaj lub zamów', step3Desc: 'Czytaj online, pobierz ePub lub zamów druk.',
            lblFormTitle: 'Ustawienia książki', lblPhoto: '📷 Zdjęcie dziecka', photoChoose: 'Kliknij, aby wybrać zdjęcie', lblDedication: '❤️ Dedykacja od rodziców', lblHeroName: 'Imię bohatera', valHeroName: 'Zosia', lblHeroAge: 'Wiek', lblStoryTheme: 'Temat bajki',
            btnCreateStory: '🪄 Wygeneruj bajkę', badgeCover: 'Rozdział 1', badgePage: 'Rozdział', defaultWelcome: 'Wypełnij formularz po lewej stronie i kliknij «Wygeneruj bajkę».',
            btnPrev: '❮ Wstecz', btnNext: 'Dalej ❯', btnAudioLbl: 'Audiobook', btnEpubLbl: 'Pobierz ePub', btnOrderLbl: 'Zamów książkę',
            lockTitle: 'Ostatni rozdział zablokowany', lockDesc: 'Odblokuj finał bajki, audiobook oraz pobieranie pliku ePub!', lockBtn: '✨ Odblokuj pełną bajkę',
            fmtModalTitle: '✨ Wybierz format książki', fmtModalSubtitle: 'W jakiej formie chcesz otrzymać bajkę?',
            fmtCard1Title: 'Czytaj online', fmtCard1Btn: 'Czytaj teraz', fmtCard2Title: 'E-book (ePub)', fmtCard2Btn: 'Pobierz ePub', fmtCard3Title: 'Książka drukowana', fmtCard3Btn: 'Zamów druk',
            statusGen: '✨ Tworzymy unikalną opowieść...', statusDone: '🎉 Książka jest gotowa!'
        },
        ro: {
            navAbout: 'Acasă', navCreate: 'Creează poveste', navLibrary: 'Cărțile mele', navOrder: 'Comandă tipar', btnAuth: '👤 Autentificare',
            heroTitle: 'Cărți terapeutice personalizate pentru copii', heroDesc: 'Încarcă poza copilului și vom crea o poveste plină de căldură.', heroCta: '🚀 Începe crearea',
            lblFormTitle: 'Setări carte', lblPhoto: '📷 Poză copil', photoChoose: 'Apasă pentru a alege poza', lblDedication: '❤️ Dedicație de la părinți', lblHeroName: 'Nume erou', valHeroName: 'Ana', lblHeroAge: 'Vârstă', lblStoryTheme: 'Tema poveștii',
            btnCreateStory: '🪄 Generează cartea', badgeCover: 'Capitolul 1', badgePage: 'Capitolul', defaultWelcome: 'Completează formularul din stânga și apasă «Generează cartea».',
            btnPrev: '❮ Înapoi', btnNext: 'Înainte ❯', btnAudioLbl: 'Audiobook', btnEpubLbl: 'Descarcă ePub', btnOrderLbl: 'Comandă cartea',
            lockTitle: 'Ultimul capitol este blocat', lockDesc: 'Deblochează finalul magic al poveștii și descărcarea ePub!', lockBtn: '✨ Deblochează cartea',
            fmtModalTitle: '✨ Alege formatul cărții', fmtModalSubtitle: 'Cum dorești să primești povestea?',
            fmtCard1Title: 'Citește online', fmtCard1Btn: 'Citește acum', fmtCard2Title: 'Carte digitală (ePub)', fmtCard2Btn: 'Descarcă ePub', fmtCard3Title: 'Carte tipărită', fmtCard3Btn: 'Comandă tipar',
            statusGen: '✨ Scriem povestea...', statusDone: '🎉 Cartea este gata!'
        },
        it: {
            navAbout: 'Home', navCreate: 'Crea favola', navLibrary: 'I miei libri', navOrder: 'Ordina stampa', btnAuth: '👤 Accedi',
            heroTitle: 'Libri terapeutici personalizzati per bambini', heroDesc: 'Carica la foto del tuo bimbo e creeremo una dolce storia illustrata.', heroCta: '🚀 Inizia a creare',
            lblFormTitle: 'Dettagli libro', lblPhoto: '📷 Foto del bimbo', photoChoose: 'Clicca per scegliere la foto', lblDedication: '❤️ Dedica dei genitori', lblHeroName: 'Nome dell\'eroe', valHeroName: 'Sofia', lblHeroAge: 'Età', lblStoryTheme: 'Tema della favola',
            btnCreateStory: '🪄 Genera il libro', badgeCover: 'Capitolo 1', badgePage: 'Capitolo', defaultWelcome: 'Compila i parametri a sinistra e clicca su «Genera il libro».',
            btnPrev: '❮ Indietro', btnNext: 'Avanti ❯', btnAudioLbl: 'Audiofavola', btnEpubLbl: 'Scarica ePub', btnOrderLbl: 'Ordina libro',
            lockTitle: 'Capitolo finale bloccato', lockDesc: 'Sblocca il magico finale della favola e il download ePub!', lockBtn: '✨ Sblocca il libro intero',
            fmtModalTitle: '✨ Scegli il formato del libro', fmtModalSubtitle: 'Come preferisci ricevere la tua favola?',
            fmtCard1Title: 'Leggi online', fmtCard1Btn: 'Leggi ora', fmtCard2Title: 'E-book digitale', fmtCard2Btn: 'Scarica ePub', fmtCard3Title: 'Libro stampato', fmtCard3Btn: 'Ordina stampa',
            statusGen: '✨ Scrittura della favola...', statusDone: '🎉 Il libro è pronto!'
        },
        tr: {
            navAbout: 'Ana Sayfa', navCreate: 'Masal Oluştur', navLibrary: 'Kitaplarım', navOrder: 'Baskı Siparişi', btnAuth: '👤 Giriş Yap / Kaydol',
            heroTitle: 'Çocuklar İçin Kişiselleştirilmiş Terapötik Kitaplar', heroDesc: 'Çocuğunuzun fotoğrafını yükleyin, eğitici ve sıcak bir masal hazırlayalım.', heroCta: '🚀 Oluşturmaya Başla',
            lblFormTitle: 'Kitap Seçenekleri', lblPhoto: '📷 Çocuk Fotoğrafı', photoChoose: 'Fotoğraf seçmek için tıklayın', lblDedication: '❤️ Aile İthafı', lblHeroName: 'Kahramanın Adı', valHeroName: 'Elif', lblHeroAge: 'Yaş', lblStoryTheme: 'Masal Teması',
            btnCreateStory: '🪄 Kitabı Oluştur', badgeCover: '1. Bölüm', badgePage: 'Bölüm', defaultWelcome: 'Soldaki alanları doldurun ve «Kitabı Oluştur» butonuna tıklayın.',
            btnPrev: '❮ Geri', btnNext: 'İleri ❯', btnAudioLbl: 'Sesli Masal', btnEpubLbl: 'ePub İndir', btnOrderLbl: 'Kitap Siparişi',
            lockTitle: 'Son Bölüm Kilitli', lockDesc: 'Masalın büyülü finalini ve ePub indirme özelliğini açın!', lockBtn: '✨ Tüm Kitabı Aç',
            fmtModalTitle: '✨ Kitap Formatını Seçin', fmtModalSubtitle: 'Masalınızı nasıl almak istersiniz?',
            fmtCard1Title: 'Çevrimiçi Oku', fmtCard1Btn: 'Şimdi Oku', fmtCard2Title: 'Dijital Kitap (ePub)', fmtCard2Btn: 'ePub İndir', fmtCard3Title: 'Basılı Kitap', fmtCard3Btn: 'Baskı Siparişi',
            statusGen: '✨ Masal yazılıyor...', statusDone: '🎉 Kitap hazır!'
        }
    };

    function changeLanguage(lang) {
        currentLang = lang;
        document.documentElement.lang = lang;
        var base = dictionary['en'];
        var d = Object.assign({}, base, dictionary[lang] || {});

        setText('nav-about', d.navAbout);
        setText('nav-create', d.navCreate);
        setText('nav-library', d.navLibrary);
        setText('nav-order', d.navOrder);
        updateAuthButtonUI();

        setText('hero-title', d.heroTitle);
        setText('hero-desc', d.heroDesc);
        setText('hero-cta', d.heroCta);

        setText('step1-title', d.step1Title);
        setText('step1-desc', d.step1Desc);
        setText('step2-title', d.step2Title);
        setText('step2-desc', d.step2Desc);
        setText('step3-title', d.step3Title);
        setText('step3-desc', d.step3Desc);

        setText('about-title', d.aboutTitle);
        setText('about-tagline', d.aboutTagline);
        setText('about-col1-title', d.aboutCol1Title);
        setText('about-col1-desc', d.aboutCol1Desc);
        setText('about-col2-title', d.aboutCol2Title);
        setText('about-col2-desc', d.aboutCol2Desc);
        setText('about-col3-title', d.aboutCol3Title);
        setText('about-col3-desc', d.aboutCol3Desc);
        setText('about-steps-title', d.aboutStepsTitle);
        setHtml('about-step-1', d.aboutStep1);
        setHtml('about-step-2', d.aboutStep2);
        setHtml('about-step-3', d.aboutStep3);

        setText('lbl-form-title', d.lblFormTitle);
        setText('lbl-photo', d.lblPhoto);
        if (!selectedPhotoFile) setText('photoStatusText', d.photoChoose);
        setText('lbl-dedication', d.lblDedication);

        setText('lbl-hero-name', d.lblHeroName);
        var heroInput = document.getElementById('heroName');
        if (heroInput) heroInput.value = d.valHeroName;

        setText('lbl-hero-age', d.lblHeroAge);
        setText('lbl-story-theme', d.lblStoryTheme);
        
        var grp1 = document.getElementById('grp-therapy');
        if (grp1) grp1.label = d.grpTherapy;
        setText('opt-theme-dark', d.themeDark);
        setText('opt-theme-school', d.themeSchool);
        setText('opt-theme-anger', d.themeAnger);
        setText('opt-theme-toys', d.themeToys);
        setText('opt-theme-share', d.themeShare);
        
        var grp2 = document.getElementById('grp-adv');
        if (grp2) grp2.label = d.grpAdv;
        setText('opt-theme-forest', d.themeForest);
        setText('opt-theme-space', d.themeSpace);
        setText('opt-theme-dino', d.themeDino);
        setText('opt-custom', d.optCustom);

        setText('lbl-art-style', d.lblArtStyle);
        setText('opt-style1', d.optStyle1);
        setText('opt-style2', d.optStyle2);
        setText('opt-style3', d.optStyle3);
        setText('lbl-consent-text', d.lblConsentText);
        setText('btn-create-story', d.btnCreateStory);

        setText('btn-prev', d.btnPrev);
        setText('btn-next', d.btnNext);
        setText('btn-audio-lbl', d.btnAudioLbl);
        setText('btn-epub-lbl', d.btnEpubLbl);
        setText('btn-order-lbl', d.btnOrderLbl);

        setText('lbl-lock-title', d.lockTitle);
        setText('lbl-lock-desc', d.lockDesc);
        setText('btn-unlock-finale', d.lockBtn);

        setText('lib-title', d.libTitle);
        setText('lib-desc', d.libDesc);

        setText('order-title', d.orderTitle);
        setText('order-desc', d.orderDesc);
        setText('lbl-order-name', d.lblOrderName);
        setText('lbl-order-email', d.lblOrderEmail);
        setText('lbl-order-phone', d.lblOrderPhone);
        setText('lbl-order-country', d.lblOrderCountry);
        setText('lbl-order-addr', d.lblOrderAddr);
        setText('lbl-order-addr2', d.lblOrderAddr2);
        setText('lbl-order-city', d.lblOrderCity);
        setText('lbl-order-postcode', d.lblOrderPostcode);
        setText('lbl-order-type', d.lblOrderType);
        setText('opt-order-hard', d.optOrderHard);
        setText('opt-order-prem', d.optOrderPrem);
        setText('btn-order-pay', d.btnOrderPay);

        setText('pay-title', d.payTitle);
        setText('pay-subtitle', d.paySubtitle);
        setText('plan1-title', d.plan1Title);
        setText('plan1-desc', d.plan1Desc);
        setText('plan1-btn', d.plan1Btn);
        setText('plan2-title', d.plan2Title);
        setText('plan2-desc', d.plan2Desc);
        setText('plan2-btn', d.plan2Btn);

        setText('fmt-modal-title', d.fmtModalTitle);
        setText('fmt-modal-subtitle', d.fmtModalSubtitle);
        setText('fmt-card1-title', d.fmtCard1Title);
        setText('fmt-card1-desc', d.fmtCard1Desc);
        setText('fmt-card1-price', d.fmtCard1Price);
        setText('fmt-card1-btn', d.fmtCard1Btn);
        setText('fmt-card2-title', d.fmtCard2Title);
        setText('fmt-card2-desc', d.fmtCard2Desc);
        setText('fmt-card2-price', d.fmtCard2Price);
        setText('fmt-card2-btn', d.fmtCard2Btn);
        setText('fmt-badge-hit', d.fmtBadgeHit);
        setText('fmt-card3-title', d.fmtCard3Title);
        setText('fmt-card3-desc', d.fmtCard3Desc);
        setText('fmt-card3-price', d.fmtCard3Price);
        setText('fmt-card3-btn', d.fmtCard3Btn);

        setText('auth-modal-title', d.authModalTitle);
        setText('auth-modal-subtitle', d.authModalSubtitle);
        setText('tab-btn-login', d.authSignIn);
        setText('tab-btn-register', d.authRegister);
        setText('lbl-auth-name', d.lblAuthName);
        setText('lbl-auth-email', d.lblAuthEmail);
        setText('lbl-auth-pass', d.lblAuthPass);
        switchAuthMode(authMode);

        if (currentPages.length > 0) {
            updateSlideUI();
        } else {
            setText('pageBadge', d.badgeCover);
            setText('currentText', d.defaultWelcome);
        }
    }

    function initFloatingBubbles(count) {
        var container = document.getElementById('bubblesContainer');
        if (!container) return;
        container.innerHTML = '';
        for (var i = 0; i < (count || 15); i++) {
            var bubble = document.createElement('div');
            bubble.className = 'bubble';
            var size = Math.floor(Math.random() * 36) + 18;
            bubble.style.width = size + 'px';
            bubble.style.height = size + 'px';
            bubble.style.left = Math.random() * 100 + '%';
            bubble.style.animationDuration = (Math.random() * 8 + 8).toFixed(1) + 's';
            bubble.style.animationDelay = (Math.random() * 8).toFixed(1) + 's';
            container.appendChild(bubble);
        }
    }

    document.addEventListener('DOMContentLoaded', function() {
        try {
            var savedUser = localStorage.getItem('storymagic_user');
            if (savedUser) currentUser = JSON.parse(savedUser);
        } catch (e) {}
        initFloatingBubbles(15);
        changeLanguage('en');
    });
