%define _topdir   .
%define buildroot ./builddir

Name:   tis-waptserver
Version:        %{_version}
Release:        1%{?dist}
Summary:        WAPT Server
BuildArch:      x86_64

Group:          Development/Tools
License:        GPL
URL:            https://wapt.fr
Source0:        ./waptserver/
Prefix:         /opt

Requires:  nginx dialog cabextract policycoreutils-python

# Turn off the brp-python-bytecompile script
%global __os_install_post %(echo '%{__os_install_post}' | sed -e 's!/usr/lib[^[:space:]]*/brp-python-bytecompile[[:space:]].*$!!g')
# to be cleanedup
%global __provides_exclude_from /
%global __requires_exclude_from /

%description

%clean
echo "No clean"

%install
set -e

mkdir -p %{buildroot}/opt/wapt
mkdir -p %{buildroot}/opt/wapt/log
mkdir -p %{buildroot}/opt/wapt/conf
mkdir -p %{buildroot}/opt/wapt/bin

mkdir -p %{buildroot}/opt/wapt/waptserver
mkdir -p %{buildroot}/opt/wapt/waptserver/scripts
ln -sf ../conf/waptserver.ini %{buildroot}/opt/wapt/waptserver/waptserver.ini

mkdir -p %{buildroot}/usr/lib/systemd/

(cd .. && python ./createrpm.py)

%files
%defattr(644,root,root,755)
/usr/lib/systemd/system/waptserver.service
/opt/wapt/waptserver/*
/opt/wapt/lib/*
/opt/wapt/lib64
/etc/logrotate.d/waptserver
/etc/rsyslog.d/waptserver.conf
/etc/systemd/system/nginx.service.d/nginx_worker_files_limit.conf
/opt/wapt/waptpackage.py
/opt/wapt/waptcrypto.py
/opt/wapt/waptutils.py
/opt/wapt/custom_zip.py
/usr/bin/wapt-serverpostconf

%attr(755,root,root)/opt/wapt/bin/*
%attr(755,root,root)/opt/wapt/waptserver/scripts/postconf.sh
%attr(755,root,root)/opt/wapt/wapt-scanpackages.py
%attr(755,root,root)/opt/wapt/wapt-signpackages.py
%attr(755,root,root)/opt/wapt/waptserver/trigger_action.sh
%attr(755,root,root)/opt/wapt/runwaptserver.sh
%attr(755,root,root)/usr/bin/wapt-scanpackages
%attr(755,root,root)/usr/bin/wapt-signpackages
%attr(755,root,root)/usr/bin/waptpython
%attr(755,wapt,root)/opt/wapt/conf
%attr(755,wapt,root)/opt/wapt/log
%attr(750,root,nginx)/opt/wapt/waptserver/ssl/
%pre
getent passwd wapt >/dev/null || \
    useradd -r -g nginx -d /opt/wapt -s /sbin/nologin \
    -c "Non privileged account for waptserver" wapt
exit 0

%post
old_ini='/opt/wapt/waptserver/waptserver.ini'
new_ini='/opt/wapt/conf/waptserver.ini'
if [ -e "$old_ini" ] && ! [ -L "$old_ini" ]; then
    if mv -n "$old_ini" "$new_ini"; then
        ln -s "$new_ini" "$old_ini"
    fi
fi
# Allow nginx to set higher limit for number of file handles
[ -f $(which setsebool) ] && setsebool -P httpd_setrlimit on
systemctl daemon-reload
mkdir -p /var/www/html/wapt
mkdir -p /var/www/html/wapt-host
mkdir -p /var/www/html/wapt-hostref
chown -R wapt:nginx /var/www/html/*
echo "User-agent:*\nDisallow: /\n" > /var/www/html/robots.txt

# fix python in wapt virtual env and set PATH
ln -sb /usr/bin/python2 /opt/wapt/bin/python2
cat << EOF > /opt/wapt/.profile
# for python virtualenv
export PYTHONHOME=/opt/wapt
export PYTHONPATH=/opt/wapt
export PATH=/opt/wapt/bin:$PATH
EOF

### end
