import pytest
import sys
import os
import re
from datetime import timedelta
from decimal import Decimal

# Чтобы импортировать app.py из корневой папки
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as app_module
from app import init_db

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATABASE", str(tmp_path / "test.db"))
    monkeypatch.setattr(app_module, "operation_notes_directory", lambda: tmp_path / "notes")
    monkeypatch.setattr(app_module, "receipts_directory", lambda: tmp_path / "receipts")
    app_module.app.config['TESTING'] = True
    init_db()
    
    with app_module.app.test_client() as client:
        yield client

def begin_top_up(client, card_id, amount):
    response = client.post(
        '/wallet/top-up',
        data={'card_id': card_id, 'amount': amount},
        follow_redirects=True,
    )
    assert response.status_code == 200
    challenge_id = int(re.search(rb'data-challenge-id="(\d+)"', response.data).group(1))
    code = re.search(rb'data-initial-code="(\d{6})"', response.data).group(1).decode()
    return response, challenge_id, code

def confirm_top_up(client, challenge_id, code):
    return client.post(
        f'/wallet/top-up/verify/{challenge_id}',
        data={'code': code},
    )

def test_generated_flask_secret_stays_stable_between_restarts(tmp_path, monkeypatch):
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    monkeypatch.setattr(app_module.app, "instance_path", str(tmp_path / "instance"))
    first_key = app_module.load_flask_secret_key()
    second_key = app_module.load_flask_secret_key()
    assert first_key == second_key
    assert len(first_key) == 64

def test_index_page(client):
    """Тест: главная страница открывается (код 200)"""
    response = client.get('/')
    assert response.status_code == 200

def test_register_and_login(client):
    """Тест: регистрация и успешный вход"""
    # Регистрация
    response = client.post('/register', data={
        'username': 'testuser',
        'email': 'test@test.com',
        'password': 'password123'
    }, follow_redirects=True)
    assert response.status_code == 200
    
    # Вход
    response = client.post('/login', data={
        'username': 'testuser',
        'password': 'password123'
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b'testuser' in response.data # Имя в шапке

def test_admin_access(client):
    """Тест: первый пользователь получает права админа и доступ к /admin"""
    client.post('/register', data={'username': 'admin', 'email': 'a@a.com', 'password': '12345'})
    client.post('/login', data={'username': 'admin', 'password': '12345'})
    
    response = client.get('/admin')
    assert response.status_code == 200
    assert b'admin' in response.data.lower()

def test_course_has_one_hundred_five_steps(client):
    response = client.get('/')
    assert response.status_code == 200
    assert b'105' in response.data

def test_each_module_has_three_practices_and_three_quizzes(client):
    with app_module.app.app_context():
        conn = app_module.get_db()
        counts = conn.execute(
            """SELECT module_id,
                      SUM(CASE WHEN step_type = 'practice' THEN 1 ELSE 0 END) AS practices,
                      SUM(CASE WHEN step_type = 'quiz' THEN 1 ELSE 0 END) AS quizzes
               FROM lessons GROUP BY module_id ORDER BY module_id"""
        ).fetchall()
        conn.close()
    assert len(counts) == 15
    assert all(row['practices'] == 3 and row['quizzes'] == 3 for row in counts)

def test_lecture_has_input_and_output_example(client):
    client.post('/register', data={'username': 'learner', 'email': 'learner@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'learner', 'password': '12345'})
    response = client.get('/lesson/m1-t')
    assert response.status_code == 200
    assert 'input()'.encode() in response.data
    assert 'Пример диалога'.encode() in response.data

def test_lesson_sidebar_lists_multiple_practices_and_tests(client):
    client.post('/register', data={'username': 'learner', 'email': 'learner@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'learner', 'password': '12345'})
    response = client.get('/lesson/m1-t')
    assert response.status_code == 200
    assert 'Практика 3'.encode() in response.data
    assert 'Тест 3'.encode() in response.data

def test_practice_validation_and_quiz_progress(client):
    client.post('/register', data={'username': 'learner', 'email': 'learner@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'learner', 'password': '12345'})

    theory = client.post('/api/submit_step', json={'slug': 'm1-t', 'answer': 'lecture'})
    assert theory.status_code == 200
    assert theory.get_json()['ok'] is True

    init_db()
    practice = client.post('/api/submit_step', json={
        'slug': 'm1-p',
        'answer': 'a = 5\nb = 3.14\nc = "Python"\nprint(a, b, c)',
    })
    assert practice.status_code == 200
    assert practice.get_json()['ok'] is True

    practice_two = client.post('/api/submit_step', json={
        'slug': 'm1-p2',
        'answer': 'year = int(input("Year: "))\nprint(2026 - year)',
    })
    assert practice_two.get_json()['ok'] is True

    practice_three = client.post('/api/submit_step', json={
        'slug': 'm1-p3',
        'answer': 'name = input("Name: ")\ncity = input("City: ")\nprint(name, city)',
    })
    assert practice_three.get_json()['ok'] is True

    quiz = client.post('/api/submit_step', json={'slug': 'm1-q', 'answer': '1'})
    assert quiz.status_code == 200
    assert quiz.get_json()['ok'] is True
    lesson_page = client.get('/lesson/m1-q')
    assert lesson_page.status_code == 200

def test_course_steps_cannot_be_submitted_out_of_order(client):
    client.post('/register', data={'username': 'learner', 'email': 'learner@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'learner', 'password': '12345'})

    response = client.post('/api/submit_step', json={'slug': 'm1-p2', 'answer': 'print("skip")'})
    assert response.status_code == 403
    assert response.get_json()['ok'] is False

def test_comments_are_saved_and_validated(client):
    client.post('/register', data={'username': 'learner', 'email': 'learner@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'learner', 'password': '12345'})

    response = client.post('/api/add_comment', json={'lesson_id': 1, 'text': 'Полезный урок'})
    assert response.status_code == 200
    assert response.get_json()['comment']['text'] == 'Полезный урок'

    invalid = client.post('/api/add_comment', json={'lesson_id': 1, 'text': '   '})
    assert invalid.status_code == 400

def test_sandbox_page_loads(client):
    response = client.get('/sandbox')
    assert response.status_code == 200
    assert b'sandbox-code' in response.data

def test_progress_can_be_reset(client):
    client.post('/register', data={'username': 'learner', 'email': 'learner@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'learner', 'password': '12345'})
    client.post('/api/submit_step', json={'slug': 'm1-t', 'answer': 'lecture'})

    response = client.post('/reset_progress', follow_redirects=True)
    assert response.status_code == 200
    assert b'0 XP' in response.data

def test_registration_issues_card_and_top_up_creates_pdf_receipt(client):
    client.post('/register', data={'username': 'walletuser', 'email': 'wallet@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'walletuser', 'password': '12345'})
    with app_module.app.app_context():
        conn = app_module.get_db()
        card = conn.execute("SELECT id, number, balance FROM cards WHERE user_id=1").fetchone()
        conn.close()
    assert len(card['number']) == 16
    assert card['balance'] == 0

    wallet_response = client.get('/wallet')
    assert wallet_response.status_code == 200
    assert 'Мои карты'.encode() in wallet_response.data
    assert card['number'][-4:].encode() in wallet_response.data
    challenge_page, challenge_id, code = begin_top_up(client, card['id'], '3000')
    assert len(code) == 6 and code.isdigit()
    assert 'Подтвердите пополнение'.encode() in challenge_page.data
    assert f'action="/wallet/top-up/verify/{challenge_id}"'.encode() in challenge_page.data
    with app_module.app.app_context():
        pending_card = app_module.db.session.get(app_module.Card, card['id'])
        assert pending_card.balance == Decimal('0.00')
        assert app_module.db.session.query(app_module.Receipt).count() == 0
    top_up = client.post(
        f'/wallet/top-up/verify/{challenge_id}',
        data={'code': code},
        follow_redirects=False,
    )
    assert top_up.status_code == 302
    receipt_id = int(top_up.headers['Location'].rsplit('/', 1)[-1])
    with app_module.app.app_context():
        receipt = app_module.db.session.get(app_module.Receipt, receipt_id)
        updated_card = app_module.db.session.get(app_module.Card, card['id'])
        assert receipt.kind == 'topup'
        assert receipt.amount == Decimal('3000.00')
        assert receipt.filename == f'receipt-{receipt_id}.pdf'
        assert updated_card.balance == Decimal('3000.00')
        assert (app_module.receipts_directory() / receipt.filename).is_file()
    receipt_page = client.get(f'/receipt/{receipt_id}')
    assert receipt_page.status_code == 200
    assert b"pushMessages" in receipt_page.data
    download = client.get(f'/receipt/{receipt_id}/download')
    assert download.status_code == 200
    assert download.mimetype == 'application/pdf'
    assert download.data.startswith(b'%PDF')

def test_top_up_requires_code_and_allows_resend_after_thirty_seconds(client):
    client.post('/register', data={'username': 'otpuser', 'email': 'otp@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'otpuser', 'password': '12345'})
    with app_module.app.app_context():
        conn = app_module.get_db()
        card = conn.execute("SELECT id FROM cards WHERE user_id=1").fetchone()
        conn.close()
    _, challenge_id, initial_code = begin_top_up(client, card['id'], '100')
    early_resend = client.post(f'/wallet/top-up/{challenge_id}/resend')
    assert early_resend.status_code == 429
    assert 1 <= early_resend.get_json()['retry_after'] <= 30

    with app_module.app.app_context():
        challenge = app_module.db.session.get(app_module.TopUpChallenge, challenge_id)
        challenge.last_sent_at = app_module.current_utc_time() - timedelta(seconds=31)
        app_module.db.session.commit()
    resent = client.post(f'/wallet/top-up/{challenge_id}/resend')
    assert resent.status_code == 200
    new_code = resent.get_json()['code']
    assert re.fullmatch(r'\d{6}', new_code)

    wrong_code = '000000' if new_code != '000000' else '111111'
    rejected = confirm_top_up(client, challenge_id, wrong_code)
    assert rejected.status_code == 200
    assert 'Неверный код'.encode() in rejected.data
    with app_module.app.app_context():
        stored_card = app_module.db.session.get(app_module.Card, card['id'])
        assert stored_card.balance == Decimal('0.00')
        assert app_module.db.session.query(app_module.Receipt).count() == 0

    confirmed = confirm_top_up(client, challenge_id, new_code)
    assert confirmed.status_code == 302
    assert '/receipt/' in confirmed.headers['Location']
    with app_module.app.app_context():
        stored_card = app_module.db.session.get(app_module.Card, card['id'])
        assert stored_card.balance == Decimal('100.00')
        assert app_module.db.session.query(app_module.Receipt).count() == 1

def test_user_can_issue_an_additional_card(client):
    client.post('/register', data={'username': 'carduser', 'email': 'card@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'carduser', 'password': '12345'})
    client.post('/wallet/cards/add')
    with app_module.app.app_context():
        cards = app_module.db.session.query(app_module.Card).filter_by(user_id=1).order_by(app_module.Card.id).all()
    assert len(cards) == 2
    assert all(len(card.number) == 16 and card.balance == Decimal('0.00') and card.is_active for card in cards)
    assert cards[0].number != cards[1].number

def test_user_cannot_top_up_another_users_card(client):
    client.post('/register', data={'username': 'cardowner', 'email': 'owner@test.com', 'password': '12345'})
    with app_module.app.app_context():
        conn = app_module.get_db()
        card = conn.execute("SELECT id FROM cards WHERE user_id=1").fetchone()
        conn.close()
    client.post('/logout')
    client.post('/register', data={'username': 'cardvisitor', 'email': 'visitor@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'cardvisitor', 'password': '12345'})
    response = client.post('/wallet/top-up', data={'card_id': card['id'], 'amount': '100'})
    assert response.status_code == 302
    with app_module.app.app_context():
        stored_card = app_module.db.session.get(app_module.Card, card['id'])
        challenge_count = app_module.db.session.query(app_module.TopUpChallenge).count()
        receipt_count = app_module.db.session.query(app_module.Receipt).count()
    assert stored_card.balance == Decimal('0.00')
    assert challenge_count == 0
    assert receipt_count == 0

def test_course_enrollment_debits_selected_card_and_unlocks_course(client):
    client.post('/register', data={'username': 'coursebuyer', 'email': 'buyer@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'coursebuyer', 'password': '12345'})
    with app_module.app.app_context():
        conn = app_module.get_db()
        card = conn.execute("SELECT id FROM cards WHERE user_id=1").fetchone()
        conn.close()

    locked_lesson = client.get('/lesson/m3-t')
    assert locked_lesson.status_code == 302
    assert '/checkout' in locked_lesson.headers['Location']

    insufficient = client.post('/checkout/1', data={'card_id': card['id']}, follow_redirects=True)
    assert insufficient.status_code == 200
    assert 'недостаточно'.encode() in insufficient.data

    _, challenge_id, code = begin_top_up(client, card['id'], '3000')
    assert confirm_top_up(client, challenge_id, code).status_code == 302
    checkout = client.post('/checkout/1', data={'card_id': card['id']})
    assert checkout.status_code == 302
    receipt_id = int(checkout.headers['Location'].rsplit('/', 1)[-1])
    receipt_page = client.get(checkout.headers['Location'])
    assert receipt_page.status_code == 200
    assert b"pushMessages" in receipt_page.data
    with app_module.app.app_context():
        enrollment = app_module.db.session.scalar(
            app_module.select(app_module.Enrollment).where(app_module.Enrollment.user_id == 1)
        )
        receipt = app_module.db.session.get(app_module.Receipt, receipt_id)
        updated_card = app_module.db.session.get(app_module.Card, card['id'])
        assert enrollment.course_id == 1
        assert receipt.kind == 'course'
        assert receipt.amount == Decimal('2990.00')
        assert updated_card.balance == Decimal('10.00')
    with app_module.app.app_context():
        conn = app_module.get_db()
        conn.execute(
            """INSERT INTO progress (user_id, lesson_id, status, attempts)
               SELECT 1, id, 'completed', 1 FROM lessons WHERE module_id <= 2"""
        )
        conn.commit()
        conn.close()
    unlocked_lesson = client.get('/lesson/m3-t')
    assert unlocked_lesson.status_code == 200
    document = client.get(f"/receipt/{receipt_id}/download")
    assert document.status_code == 200
    assert document.data.startswith(b'%PDF')

def test_checkout_page_lists_owned_internal_cards(client):
    client.post('/register', data={'username': 'secureuser', 'email': 'secure@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'secureuser', 'password': '12345'})
    checkout = client.get('/course/1/checkout')
    assert checkout.status_code == 200
    assert 'Карта для оплаты'.encode() in checkout.data
    assert '₽'.encode() in checkout.data
    assert b'name="card_id"' in checkout.data

def test_receipt_is_only_visible_to_its_owner(client):
    client.post('/register', data={'username': 'receiptowner', 'email': 'owner@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'receiptowner', 'password': '12345'})
    with app_module.app.app_context():
        conn = app_module.get_db()
        card = conn.execute("SELECT id FROM cards WHERE user_id=1").fetchone()
        conn.close()
    _, challenge_id, code = begin_top_up(client, card['id'], '100')
    client.get('/logout')

    client.post('/register', data={'username': 'otheruser', 'email': 'other@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'otheruser', 'password': '12345'})
    assert client.get(f'/wallet/top-up/verify/{challenge_id}').status_code == 404
    assert client.post(f'/wallet/top-up/{challenge_id}/resend').status_code == 404
    assert confirm_top_up(client, challenge_id, code).status_code == 404

def test_invalid_top_up_amount_does_not_create_receipt(client):
    client.post('/register', data={'username': 'amountuser', 'email': 'amount@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'amountuser', 'password': '12345'})
    with app_module.app.app_context():
        conn = app_module.get_db()
        card = conn.execute("SELECT id FROM cards WHERE user_id=1").fetchone()
        conn.close()
    for amount in ('99', '50001', '100.501', 'NaN', 'invalid'):
        response = client.post('/wallet/top-up', data={'card_id': card['id'], 'amount': amount})
        assert response.status_code == 302
        assert response.headers['Location'].endswith('/wallet')
    with app_module.app.app_context():
        count = app_module.db.session.query(app_module.Receipt).count()
    assert count == 0

def test_fractional_card_balance_is_kept_as_decimal(client):
    client.post('/register', data={'username': 'decimaluser', 'email': 'decimal@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'decimaluser', 'password': '12345'})
    with app_module.app.app_context():
        conn = app_module.get_db()
        card = conn.execute("SELECT id FROM cards WHERE user_id=1").fetchone()
        conn.close()
    _, challenge_id, code = begin_top_up(client, card['id'], '100.25')
    response = confirm_top_up(client, challenge_id, code)
    assert response.status_code == 302
    with app_module.app.app_context():
        stored_card = app_module.db.session.get(app_module.Card, card['id'])
        assert stored_card.balance == Decimal('100.25')
    balance_page = client.get('/wallet')
    assert '100.25 ₽'.encode() in balance_page.data

def test_pdf_generation_failure_rolls_back_card_top_up(client, monkeypatch):
    client.post('/register', data={'username': 'rollbackuser', 'email': 'rollback@test.com', 'password': '12345'})
    client.post('/login', data={'username': 'rollbackuser', 'password': '12345'})
    with app_module.app.app_context():
        conn = app_module.get_db()
        card = conn.execute("SELECT id FROM cards WHERE user_id=1").fetchone()
        conn.close()
    monkeypatch.setattr(app_module, 'make_receipt_pdf', lambda receipt, user: (_ for _ in ()).throw(RuntimeError('PDF failure')))
    _, challenge_id, code = begin_top_up(client, card['id'], '100')
    with pytest.raises(RuntimeError, match='PDF failure'):
        confirm_top_up(client, challenge_id, code)
    with app_module.app.app_context():
        stored_card = app_module.db.session.get(app_module.Card, card['id'])
        receipts = app_module.db.session.query(app_module.Receipt).count()
    assert stored_card.balance == Decimal('0.00')
    assert receipts == 0