package memu.warden

default allow = false

# Allow if the user role is admin
allow {
    input.user.role == "admin"
}

# Allow if the action is read and the user is authenticated
allow {
    input.action == "read"
    input.user.authenticated == true
}
