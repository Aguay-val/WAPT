#!/bin/sh
set -ex
VERSION=$(python ../../waptserver/rpm/get_version.py ../../waptserver/waptserver_config.py)
mkdir -p BUILD BUILDROOT RPMS
QA_SKIP_BUILD_ROOT=1 rpmbuild -bb --define "_version $VERSION" --buildroot $PWD/BUILDROOT -v --clean waptrepo.spec 1>&2
rm -f tis-waptrepo.rpm
cp RPMS/x86_64/tis-waptrepo*.rpm .
echo tis-waptrepo-*.rpm
