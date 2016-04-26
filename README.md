# DDFacet

## Dependencies

From an Ubuntu 14.04 base:

```
sudo apt-get install git python-pip python-casacore libfftw3-dev python-pyephem python-numexpr cython cmake python-meqtrees-cattery
sudo pip install SharedArray
sudo pip install Polygon2
sudo pip install pyFFTW
```

Then need to clone or checkout the following three:

```
git clone git@github.com:cyriltasse/SkyModel.git
git clone git@github.com:cyriltasse/killMS2.git
(cd killMS2; git checkout NewImPredict ) ## for now...
git clone git@github.com:cyriltasse/DDFacet.git

```

## Build

Build a few libraries:

```

(cd DDFacet/ ; mkdir cbuild ; cd cbuild ; cmake -DCMAKE_BUILD_TYPE=Release .. ; make)
# or -DCMAKE_BUILD_TYPE=RelWithDebInfo for developers: this includes debugging symbols
(cd ./killMS2/Predict ; make)
(cd ./killMS2/Predict ; make)
(cd killMS2/Array/Dot ; make)
```

## Paths etc.

Add this to your ``.bashrc``

```
export KILLMS_DIR=$HOME/projects   ### or whereever you've git cloned the repos
export DDFACET_DIR=$KILLMS_DIR
export PYTHONPATH=$PYTHONPATH:$KILLMS_DIR
export LD_LIBRARY_PATH=$KILLMS_DIR/DDFacet/cbuild/Gridder:$LD_LIBRARY_PATH
export PATH=$KILLMS_DIR/killMS2:$KILLMS_DIR/SkyModel:$KILLMS_DIR/DDFacet:$PATH
export DDFACET_TEST_DATA_DIR=[folder where you keep the acceptance test data and images]
export DDFACET_TEST_OUTPUT_DIR=[folder where you want the acceptance test output to be dumped]
```

## Acceptance tests
Most of the core use cases will in the nearby future have reference images and an automated acceptance test.

###To test your branch against the master branch using Jenkins
Please **do not** commit against cyriltasse/master. The correct strategy is to branch/fork and do a pull request on Github
to merge changes into master. Once you opened a pull request add the following comment: "ok to test". This will let the Jenkins server know to start testing. You should see that the pull request and commit statusses shows "Pending". If the test succeeds you should see "All checks have passed" above the green merge button. Once the code is reviewed it will be merged into the master branch.

###To run the tests on your local machine:
You can run the automated tests by grabbing the latest set of measurements and reference images from the web and
extracting them to the directory you set up in your **DDFACET_TEST_DATA_DIR** environment variable. You can run 
the automated tests by navigating to your DDFacet directory and running nosetests. 

Each of the test cases is labeled by a class name and has reference images and a parset file with the same
name, ie. if the test case that has failed is called "TestWidefieldDirty" the reference images will be called the same. You should investigate the reason for any severe discrepancies between the output of the test case and the images produced by your changed codebase. See the docstring at the top of the class ClassCompareFITSImage for help and
filename conventions.

Acceptance test data can be found on the Jenkins server in the **/data/test-data** directory. 

###Adding more tests and creating new reference images.

To resimulate images and add more tests:
In the Jenkins server data directory run **make** to resimulate and set up new reference images. This should only be done with the **origin/master** branch - not your branch or fork! You should manually verify that all the reference images are correct when you regenerate them. Each time you add a new option to DDFacet also add an option to the makefile in this directory. Once the option is set up in the makefile you can build the reference images on Jenkins.