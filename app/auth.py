from flask import Blueprint, render_template, redirect, url_for, request, jsonify
from flask_login import login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from db import *
from flask_login import LoginManager
import activity
from utils import client_address
from settings import get_settings

import logging
import re

# Retrieve main logger
logger = logging.getLogger('main')

def validate_password(password):
    """
    Validate password according to Basic Auth specifications and Tinfoil compatibility.
    Rejects:
    - Empty passwords
    - Control characters (null, line breaks, etc.)
    - Tinfoil forbidden characters: @ & / ? # =
    - Non-UTF-8 characters
    """
    if not password:
        return False, "Password cannot be empty"
    
    # Check for control characters (null, line breaks, etc.)
    # This includes: null (\x00), tab (\x09), newline (\x0A), carriage return (\x0D), and other control chars
    control_chars = r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]'
    if re.search(control_chars, password):
        return False, "Password contains invalid control characters"
    
    # Also check for tab, newline, and carriage return explicitly
    if '\t' in password or '\n' in password or '\r' in password:
        return False, "Password contains invalid control characters"
    
    # Check for Tinfoil forbidden characters
    tinfoil_forbidden = r'[@&/?#=]'
    if re.search(tinfoil_forbidden, password):
        return False, "Password contains invalid characters. Please avoid: @ & / ? # ="
    
    # Check for UTF-8 encoding validity
    try:
        password.encode('utf-8')
    except UnicodeEncodeError:
        return False, "Password contains invalid UTF-8 characters"
    
    return True, "Password is valid"

def validate_username(username):
    """
    Validate username according to Basic Auth specifications.
    Rejects:
    - Empty usernames
    - Colons (:)
    - Control characters (null, line breaks, etc.)
    - Non-UTF-8 characters
    """
    if not username:
        return False, "Username cannot be empty"
    
    # Check for colons (not allowed in Basic Auth usernames)
    if ':' in username:
        return False, "Username cannot contain colons (:)"
    
    # Check for control characters (null, line breaks, etc.)
    control_chars = r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]'
    if re.search(control_chars, username):
        return False, "Username contains invalid control characters"
    
    # Also check for tab, newline, and carriage return explicitly
    if '\t' in username or '\n' in username or '\r' in username:
        return False, "Username contains invalid control characters"
    
    # Check for UTF-8 encoding validity
    try:
        username.encode('utf-8')
    except UnicodeEncodeError:
        return False, "Username contains invalid UTF-8 characters"
    
    return True, "Username is valid"

def admin_account_created():
    return len(User.query.filter_by(admin_access=True).all())

def unauthorized_json():
    response = login_manager.unauthorized()
    resp = {
        'success': False,
        'status_code': response.status_code,
        'location': response.location
    }
    return jsonify(resp)

def shop_access_error():
    """None if the requester has shop access (public shop, or authenticated user with
    shop_access); otherwise the Flask response to return instead.

    Shared by routes that follow the shop's access policy but render their own template,
    so they can't just be wrapped in @access_required('shop') like the shop routes are."""
    if not get_settings()['shop']['public'] and admin_account_created():
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not current_user.has_shop_access():
            return 'Forbidden', 403
    return None


def access_required(access: str):
    def _access_required(f):
        @wraps(f)
        def decorated_view(*args, **kwargs):
            if not admin_account_created():
                # Auth disabled, request ok
                return f(*args, **kwargs)

            if not current_user.is_authenticated:
                # return unauthorized_json()
                return login_manager.unauthorized()

            if not current_user.has_access(access):
                return 'Forbidden', 403
            return f(*args, **kwargs)
        return decorated_view
    return _access_required


def basic_auth(request):
    success = True
    # Deliberately identical for "unknown user" and "wrong password": distinguishing
    # them lets a caller enumerate valid usernames against this endpoint.
    error = 'Invalid credentials.'
    user = None

    auth = request.authorization
    if auth is None:
        success = False
        error = 'No authentication provided.'
        return success, error, user

    username = auth.username
    password = auth.password
    user = User.query.filter_by(user=username).first()
    if user is None:
        success = False
        logger.info(f'Basic auth failed: unknown user {username}.')

    elif not check_password_hash(user.password, password):
        success = False
        logger.info(f'Basic auth failed: incorrect password for user {username}.')

    return success, error, user

auth_blueprint = Blueprint('auth', __name__)

login_manager = LoginManager()
login_manager.login_view = 'auth.login'

def create_or_update_user(username, password, admin_access=False, shop_access=False, backup_access=False):
    """
    Create a new user or update an existing user with the given credentials and access rights.
    """
    # Validate username before creating/updating user
    is_valid, error_message = validate_username(username)
    if not is_valid:
        logger.error(f'Username validation failed for user {username}: {error_message}')
        raise ValueError(f"Username validation failed: {error_message}")
    
    # Validate password before creating/updating user
    is_valid, error_message = validate_password(password)
    if not is_valid:
        logger.error(f'Password validation failed for user {username}: {error_message}')
        raise ValueError(f"Password validation failed: {error_message}")
    
    user = User.query.filter_by(user=username).first()
    if user:
        logger.info(f'Updating existing user {username}')
        user.admin_access = admin_access
        user.shop_access = shop_access
        user.backup_access = backup_access
        user.password = generate_password_hash(password, method='scrypt')
    else:
        logger.info(f'Creating new user {username}')
        new_user = User(user=username, password=generate_password_hash(password, method='scrypt'), admin_access=admin_access, shop_access=shop_access, backup_access=backup_access)
        db.session.add(new_user)
    db.session.commit()

def init_user_from_environment(environment_name, admin=False):
    """
    allow to init some user from environment variable to init some users without using the UI
    """
    username = os.getenv(environment_name + '_NAME')
    password = os.getenv(environment_name + '_PASSWORD')
    if username and password:
        if admin:
            logger.info('Initializing an admin user from environment variable...')
            admin_access = True
            shop_access = True
            backup_access = True
        else:
            logger.info('Initializing a regular user from environment variable...')
            admin_access = False
            shop_access = True
            backup_access = False

        if not admin:
            existing_admin = admin_account_created()
            if not existing_admin and not admin_access:
                logger.error(f'Error creating user {username}, first account created must be admin')
                return

        create_or_update_user(username, password, admin_access, shop_access, backup_access)

def init_users(app):
    with app.app_context():
        # init users from ENV
        if os.environ.get('USER_ADMIN_NAME') is not None:
            init_user_from_environment(environment_name="USER_ADMIN", admin=True)
        if os.environ.get('USER_GUEST_NAME') is not None:
            init_user_from_environment(environment_name="USER_GUEST", admin=False)

@auth_blueprint.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        next_url = request.args.get('next', '')
        if current_user.is_authenticated:
            return redirect(next_url if len(next_url) else '/')
        return render_template('login.html', title='Login')
        
    # login code goes here
    username = request.form.get('user')
    password = request.form.get('password')
    remember = bool(request.form.get('remember'))
    next_url = request.args.get('next', '')

    user = User.query.filter_by(user=username).first()

    # check if the user actually exists
    # take the user-supplied password, hash it, and compare it to the hashed password in the database
    if not user or not check_password_hash(user.password, password):
        logger.warning(f'Incorrect login for user {username}')
        activity.record_login(username, success=False, ip=client_address(request),
                              detail='invalid credentials')
        return redirect(url_for('auth.login')) # if the user doesn't exist or password is wrong, reload the page

    # if the above check passes, then we know that the user has the right credentials
    logger.info(f'Sucessfull login for user {username}')
    activity.record_login(username, success=True, ip=client_address(request))
    login_user(user, remember=remember)

    return redirect(next_url if len(next_url) else '/')

@auth_blueprint.route('/profile')
@login_required
@access_required('backup')
def profile():
    return render_template('profile.html')

@auth_blueprint.route('/api/users')
@access_required('admin')
def get_users():
    all_users = [
        dict(db_user._mapping)
        for db_user in db.session.query(User.id, User.user, User.admin_access, User.shop_access, User.backup_access).all()
    ]
    return jsonify(all_users)

@auth_blueprint.route('/api/user', methods=['DELETE'])
@login_required
@access_required('admin')
def delete_user():
    success = True
    error = None
    data = request.json
    user_id = data['user_id']
    try:
        user = User.query.filter_by(id=user_id).first()
        if user is None:
            success = False
            error = 'User not found.'
        elif user.admin_access and User.query.filter_by(admin_access=True).count() <= 1:
            # Deleting the last admin would drop admin_account_created() to 0, which
            # disables access_required() for every route (including signup) app-wide.
            success = False
            error = 'Cannot delete the last remaining admin account.'
        else:
            User.query.filter_by(id=user_id).delete()
            db.session.commit()
            logger.info(f'Successfully deleted user with id {user_id}.')
    except Exception as e:
        logger.error(f'Could not delete user with id {user_id}: {e}')
        success = False
        error = str(e)

    resp = {
        'success': success
    }
    if error:
        resp['error'] = error
    return jsonify(resp)

@auth_blueprint.route('/api/user/signup', methods=['POST'])
@access_required('admin')
def signup_post():
    signup_success = True
    data = request.json

    username = data['user']
    password = data['password']
    admin_access = data['admin_access']
    if admin_access:
        shop_access = True
        backup_access = True
    else:
        shop_access = data['shop_access']
        backup_access = data['backup_access']

    # Validate username first
    is_valid, error_message = validate_username(username)
    if not is_valid:
        logger.error(f'Username validation failed for user {username}: {error_message}')
        resp = {
            'success': False,
            'error': f"Username validation failed: {error_message}"
        }
        return jsonify(resp)

    # Validate password
    is_valid, error_message = validate_password(password)
    if not is_valid:
        logger.error(f'Password validation failed for user {username}: {error_message}')
        resp = {
            'success': False,
            'error': f"Password validation failed: {error_message}"
        }
        return jsonify(resp)

    user = User.query.filter_by(user=username).first() # if this returns a user, then the user already exists in database
    
    if user: # if a user is found, we want to redirect back to signup page so user can try again
        logger.error(f'Error creating user {username}, user already exists')
        resp = {
            'success': False,
            'error': 'User already exists'
        }
        return jsonify(resp)
    
    existing_admin = admin_account_created()
    if not existing_admin and not admin_access:
        logger.error(f'Error creating user {username}, first account created must be admin')
        resp = {
            'success': False,
            'status_code': 400,
            'location': '/admin/settings',
        } 
        return jsonify(resp)

    try:
        # create a new user with the form data. Hash the password so the plaintext version isn't saved.
        create_or_update_user(username, password, admin_access, shop_access, backup_access)
        logger.info(f'Successfully created user {username}.')
    except ValueError as e:
        # This should not happen since we validate above, but just in case
        resp = {
            'success': False,
            'error': str(e)
        }
        return jsonify(resp)

    resp = {
        'success': signup_success
    } 

    if not existing_admin and admin_access:
        logger.debug('First admin account created')
        resp['status_code'] = 302,
        resp['location'] = '/admin/settings'
    
    return jsonify(resp)


@auth_blueprint.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/')