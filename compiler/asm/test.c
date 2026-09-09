
int foo(int x){//frame 2
    int b = 3;
    return x*b;
}

//frame 1
int main(){
    int x = 5;
    int a = foo(x);
    return 0;
}