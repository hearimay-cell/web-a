/**
 * 해아림한의원 안양점 인터랙티브 자바스크립트 (main.js)
 */

document.addEventListener('DOMContentLoaded', () => {
  // 1. 5대 특화 클리닉 탭 전환 기능
  const clinicTabBtns = document.querySelectorAll('.clinic-tab-btn');
  const clinicTabPanes = document.querySelectorAll('.clinic-tab-pane');

  function activateClinicTab(targetId) {
    clinicTabBtns.forEach(btn => {
      if (btn.dataset.tab === targetId) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });

    clinicTabPanes.forEach(pane => {
      if (pane.id === targetId) {
        pane.classList.add('active');
      } else {
        pane.classList.remove('active');
      }
    });
  }

  clinicTabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const tabId = btn.dataset.tab;
      activateClinicTab(tabId);
    });
  });

  // 퀵 클리닉 배너 카드 클릭 시 해당 탭 활성화 및 스크롤 이동
  const quickCards = document.querySelectorAll('.quick-clinic-card');
  quickCards.forEach(card => {
    card.addEventListener('click', (e) => {
      const targetTab = card.dataset.targetTab;
      if (targetTab) {
        activateClinicTab(targetTab);
      }
    });
  });

  // 2. 자주 묻는 질문 (FAQ) 아코디언 토글 기능
  const faqItems = document.querySelectorAll('.faq-accordion-item');
  faqItems.forEach(item => {
    const questionBtn = item.querySelector('.faq-question-btn');
    if (questionBtn) {
      questionBtn.addEventListener('click', () => {
        const isActive = item.classList.contains('active');
        // 다른 열린 아코디언 닫기
        faqItems.forEach(otherItem => otherItem.classList.remove('active'));
        // 클릭한 항목 토글
        if (!isActive) {
          item.classList.add('active');
        }
      });
    }
  });

  // 3. 미디어 & 칼럼 카테고리 필터링
  const mediaFilterBtns = document.querySelectorAll('.media-filter-btn');
  const mediaCards = document.querySelectorAll('.media-card');

  mediaFilterBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      mediaFilterBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      const filter = btn.dataset.filter;
      mediaCards.forEach(card => {
        if (filter === 'all' || card.dataset.category === filter) {
          card.style.display = 'flex';
        } else {
          card.style.display = 'none';
        }
      });
    });
  });

  // 4. 스무스 스크롤 네비게이션 보정
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function(e) {
      const href = this.getAttribute('href');
      if (href.length > 1) {
        const targetElem = document.querySelector(href);
        if (targetElem) {
          e.preventDefault();
          const offsetTop = targetElem.getBoundingClientRect().top + window.pageYOffset - 80;
          window.scrollTo({
            top: offsetTop,
            behavior: 'smooth'
          });
        }
      }
    });
  });
});
